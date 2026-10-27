"""Offline regression checks for resources retained across repeated jobs."""
import asyncio
from contextlib import suppress
import gc
import os
from pathlib import Path
import sys
import tempfile
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessage

from app.bot import Config, VideoBot
from app.downloader import DownloadError, TooLarge, download


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        disk = patch("app.downloader.shutil.disk_usage",
                     return_value=SimpleNamespace(free=100_000_000_000))
        disk.start()
        self.addCleanup(disk.stop)

    async def test_worker_memory_and_temp_files_stay_bounded_across_240_jobs(self):
        service = VideoBot(Config("123:fake", frozenset({123}), "http://metube", 100_000, 600))
        outcome = 0
        completed = 0

        async def nothing(*args, **kwargs):
            return None

        async def send(*args, **kwargs):
            nonlocal completed
            completed += 1
            if outcome == 5:
                raise TelegramAPIError(method=SendMessage(chat_id=123, text="test"), message="refused")

        async def fake_download(url, root, **kwargs):
            path = root / ("video.webm" if outcome == 1 else "video.mp4")
            path.write_bytes(b"x" * 64_000)
            if outcome == 2:
                raise TooLarge()
            if outcome == 3:
                raise TimeoutError()
            if outcome == 4:
                raise DownloadError("unavailable")
            return path

        status = SimpleNamespace(edit_text=nothing)
        message = SimpleNamespace(answer_video=send, answer_document=send)
        tempdir_factory = tempfile.TemporaryDirectory

        with tempdir_factory() as storage:
            root = Path(storage)
            with patch("app.bot.download", new=fake_download), \
                    patch("app.bot.tempfile.TemporaryDirectory",
                          new=lambda **kwargs: tempdir_factory(dir=root, **{k: v for k, v in kwargs.items() if k != "dir"})), \
                    patch("app.bot.log.warning", new=lambda *args, **kwargs: None):
                task = asyncio.create_task(service.worker())

                async def batch(count):
                    nonlocal outcome
                    for i in range(count):
                        outcome = i % 6
                        service.pending.add(123)
                        service.queue.put_nowait((message, status, "https://youtu.be/test", 123))
                        await asyncio.wait_for(service.queue.join(), 2)
                        self.assertFalse(service.pending)
                        self.assertEqual(list(root.iterdir()), [])
                    gc.collect()
                    await asyncio.sleep(0)

                try:
                    # Warm asyncio, temporary-file and error-handling caches before measuring.
                    await batch(24)
                    tracemalloc.start()
                    gc.collect()
                    before = tracemalloc.get_traced_memory()[0]
                    await batch(120)
                    middle = tracemalloc.get_traced_memory()[0]
                    await batch(120)
                    after, peak = tracemalloc.get_traced_memory()
                    # Generous bounds tolerate allocator/cache differences across Python builds.
                    self.assertLess(after - before, 1_000_000)
                    self.assertLess(after - middle, 500_000)
                    self.assertLess(peak - before, 4_000_000)
                    self.assertFalse(task.done())
                    self.assertEqual(completed, 132)  # 3 upload attempts per 6 outcomes
                finally:
                    tracemalloc.stop()
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    @unittest.skipUnless(Path("/proc/self/fd").is_dir(), "Linux /proc required")
    async def test_repeated_downloads_leave_no_child_processes_or_open_pipes(self):
        children_file = Path(f"/proc/self/task/{os.getpid()}/children")

        def children():
            return set(children_file.read_text().split())

        baseline_children = children()
        baseline_fds = len(list(Path("/proc/self/fd").iterdir()))
        with tempfile.TemporaryDirectory() as storage:
            root = Path(storage)
            for i in range(18):
                with tempfile.TemporaryDirectory(dir=root) as tmp:
                    work = Path(tmp)
                    if i % 3 == 0:
                        script = (
                            "import json,pathlib,sys; p=pathlib.Path(sys.argv[1])/'video.mp4';"
                            "p.write_bytes(b'video'); print(json.dumps(str(p)))"
                        )
                    elif i % 3 == 1:
                        script = "import sys; sys.stderr.write('unavailable'); sys.exit(1)"
                    else:
                        script = "import time; time.sleep(10)"
                    with patch("app.downloader.make_command", side_effect=lambda url, directory, *args, **kwargs:
                               [sys.executable, "-c", script, str(directory)]):
                        try:
                            await download("https://youtu.be/test", work, timeout=0.1 if i % 3 == 2 else 3)
                        except (DownloadError, TimeoutError):
                            if i % 3 == 0:
                                raise
                self.assertEqual(list(root.iterdir()), [])
                self.assertEqual(children(), baseline_children)
        gc.collect()
        await asyncio.sleep(0)
        self.assertLessEqual(len(list(Path("/proc/self/fd").iterdir())), baseline_fds + 2)
