"""Regressions from review of 3c72e75: exercise outcomes, not just executed lines."""
import asyncio
from contextlib import suppress
import os
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramAPIError
from aiogram.methods import EditMessageText
from yt_dlp import YoutubeDL, parse_options

from app.bot import Config, VideoBot
from app.downloader import download, make_command, work_size


class ReviewRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_dead_parent_with_child_holding_pipes_cannot_stall_next_job(self):
        service = VideoBot(Config("123:fake", frozenset({123, 456}), "http://metube", 50_000_000, 0.4))
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp)
            pgid_file = control / "pgid"
            orphan = (
                "import os,time,pathlib; "
                "pathlib.Path(" + repr(str(pgid_file)) + ").write_text(str(os.getpid())); "
                "pid=os.fork(); "
                "time.sleep(30) if pid==0 else os._exit(0)"
            )
            success = (
                "import pathlib,sys,json; p=pathlib.Path(sys.argv[1])/'video.mp4'; "
                "p.write_bytes(b'video'); print(json.dumps(str(p)))"
            )

            def command(url, directory, *args, **kwargs):
                return [sys.executable, "-c", orphan if url.endswith("first") else success, str(directory)]

            first = SimpleNamespace(answer_video=AsyncMock(), answer_document=AsyncMock())
            second = SimpleNamespace(answer_video=AsyncMock(), answer_document=AsyncMock())
            statuses = [SimpleNamespace(edit_text=AsyncMock()) for _ in range(2)]
            service.pending.update({123, 456})
            service.queue.put_nowait((first, statuses[0], "https://youtu.be/first", 123))
            service.queue.put_nowait((second, statuses[1], "https://youtu.be/second", 456))
            with patch("app.downloader.make_command", side_effect=command):
                worker = asyncio.create_task(service.worker())
                drained = asyncio.create_task(service.queue.join())
                try:
                    done, _ = await asyncio.wait({drained}, timeout=2)
                    self.assertIn(drained, done, "Timeout cleanup stalled the entire queue")
                    first.answer_video.assert_not_awaited()
                    second.answer_video.assert_awaited_once()
                    self.assertFalse(service.pending)
                finally:
                    # Independent safety net: do not hang the test suite on the old bug.
                    if pgid_file.exists():
                        with suppress(ProcessLookupError):
                            os.killpg(int(pgid_file.read_text()), signal.SIGKILL)
                    worker.cancel()
                    done, _ = await asyncio.wait({worker}, timeout=2)
                    if worker in done:
                        with suppress(asyncio.CancelledError):
                            await worker
                    drained.cancel()
                    with suppress(asyncio.CancelledError):
                        await drained

    async def test_deleted_status_does_not_prevent_download_or_upload(self):
        for fails_on in ["Скачиваю видео…", "Отправляю видео…", "all"]:
            service = VideoBot(Config("123:fake", frozenset({123}), "http://metube", 100, 600))
            message = SimpleNamespace(answer_video=AsyncMock(), answer_document=AsyncMock())

            async def edit(text):
                if fails_on in (text, "all"):
                    raise TelegramAPIError(
                        method=EditMessageText(text=text, chat_id=123, message_id=10),
                        message="message to edit not found",
                    )

            status = SimpleNamespace(edit_text=AsyncMock(side_effect=edit))
            paths = []

            async def fake_download(url, root, **kwargs):
                path = root / "video.mp4"
                path.write_bytes(b"video")
                paths.append(path)
                return path

            service.pending.add(123)
            service.queue.put_nowait((message, status, "https://youtu.be/test", 123))
            with patch("app.bot.download", new=fake_download):
                worker = asyncio.create_task(service.worker())
                try:
                    await asyncio.wait_for(service.queue.join(), 2)
                    message.answer_video.assert_awaited_once()
                    self.assertEqual(len(paths), 1)
                    self.assertFalse(paths[0].parent.exists())
                finally:
                    worker.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker


class FileAndFormatRegressions(unittest.TestCase):
    def test_file_disappears_during_disk_accounting(self):
        vanished = SimpleNamespace(is_file=lambda: True)
        vanished.stat = lambda: (_ for _ in ()).throw(FileNotFoundError("renamed"))
        with tempfile.TemporaryDirectory() as tmp:
            stable = Path(tmp) / "audio.m4a"
            stable.write_bytes(b"1234")
            directory = SimpleNamespace(iterdir=lambda: iter([vanished, stable]))
            self.assertEqual(work_size(directory), 4)

    def test_real_ytdlp_selects_smaller_format_when_720p_exceeds_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.downloader.Path.is_file", return_value=False):
                cmd = make_command("https://youtu.be/test", Path(tmp), "youtube", 50_000_000)
            options = parse_options(cmd[3:]).ydl_opts
            info = {
                "id": "fixture", "title": "Size fixture", "extractor": "fixture",
                "formats": [
                    {"format_id": "480", "url": "https://cdn.example/480.mp4",
                     "ext": "mp4", "height": 480, "width": 854,
                     "vcodec": "h264", "acodec": "aac", "filesize": 20_000_000},
                    {"format_id": "720", "url": "https://cdn.example/720.mp4",
                     "ext": "mp4", "height": 720, "width": 1280,
                     "vcodec": "h264", "acodec": "aac", "filesize": 60_000_000},
                ],
            }
            with YoutubeDL(options) as ydl:
                selected = ydl.process_ie_result(info, download=False)
            self.assertEqual(selected["format_id"], "480")


class AdditionalBoundaryRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_sizes_and_merged_track_total_retry_lower_quality(self):
        for reason in ["skipped", "merged"]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                heights = []

                def command(url, directory, *args, height=720, **kwargs):
                    heights.append(height)
                    self.assertEqual(len(list(root.glob("attempt-*"))), 1)
                    if height == 720 and reason == "skipped":
                        script = "pass"  # yt-dlp skips oversized files with exit code zero.
                    else:
                        size = 60 if height == 720 else 20
                        script = (
                            "import pathlib,sys,json; root=pathlib.Path(sys.argv[1]);"
                            "(root/'video.f1.mp4').write_bytes(b'x'*40);"
                            "(root/'video.f2.m4a').write_bytes(b'x'*20);"
                            "p=root/'video.mp4'; p.write_bytes(b'x'*" + str(size) + ");"
                            "print(json.dumps(str(p)))"
                        )
                    return [sys.executable, "-c", script, str(directory)]

                with patch("app.downloader.make_command", side_effect=command):
                    result = await download("https://youtu.be/test", root, limit=50)
                self.assertEqual(heights, [720, 480])
                self.assertEqual(result.stat().st_size, 20)
                self.assertEqual(list(root.iterdir()), [result])

    async def test_quality_attempts_share_one_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            heights = []

            def command(url, directory, *args, height=720, **kwargs):
                heights.append(height)
                script = (
                    "import time,pathlib,sys,json; time.sleep(0.1);"
                    "p=pathlib.Path(sys.argv[1])/'video.mp4';"
                    "p.write_bytes(b'x'*60); print(json.dumps(str(p)))"
                )
                return [sys.executable, "-c", script, str(directory)]

            with patch("app.downloader.make_command", side_effect=command):
                with self.assertRaises(TimeoutError):
                    await download("https://youtu.be/test", root, limit=50, timeout=0.4)
            self.assertLess(len(heights), 5)
            self.assertEqual(list(root.iterdir()), [])

    async def test_cleanup_of_stuck_wait_and_reader_has_a_deadline(self):
        from unittest.mock import MagicMock
        from app.downloader import stop_process

        async def blocked():
            await asyncio.Event().wait()

        for has_transport in [False, True]:
            proc = SimpleNamespace(pid=123, returncode=0, wait=blocked)
            if has_transport:
                proc._transport = MagicMock()
            reader = asyncio.create_task(blocked())
            with patch("app.downloader.os.killpg") as kill, patch("app.downloader.CLEANUP_TIMEOUT", 0.01):
                await asyncio.wait_for(stop_process(proc, reader), 0.3)
            kill.assert_called_once_with(123, signal.SIGKILL)
            self.assertTrue(reader.cancelled())
            if has_transport:
                proc._transport.close.assert_called_once()

    async def test_cancellation_during_cleanup_waits_for_bounded_cleanup(self):
        import app.downloader as downloader
        started, release = asyncio.Event(), asyncio.Event()
        original = downloader.stop_process

        async def delayed_cleanup(proc, reader=None):
            await original(proc, reader)
            started.set()
            await release.wait()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = (
                "import pathlib,sys,json; p=pathlib.Path(sys.argv[1])/'video.mp4';"
                "p.write_bytes(b'video'); print(json.dumps(str(p)))"
            )

            def command(url, directory, *args, **kwargs):
                return [sys.executable, "-c", script, str(directory)]

            with patch("app.downloader.make_command", side_effect=command), \
                    patch("app.downloader.stop_process", new=delayed_cleanup):
                task = asyncio.create_task(download("https://youtu.be/test", root))
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
            self.assertEqual(list(root.iterdir()), [])

    async def test_stalled_status_updates_do_not_block_video(self):
        service = VideoBot(Config("123:fake", frozenset({123}), "http://metube", 100, 600))
        message = SimpleNamespace(answer_video=AsyncMock(), answer_document=AsyncMock())

        async def blocked_edit(*args, **kwargs):
            await asyncio.Event().wait()

        status = SimpleNamespace(edit_text=blocked_edit)

        async def fake_download(url, root, **kwargs):
            path = root / "video.mp4"
            path.write_bytes(b"video")
            return path

        service.pending.add(123)
        service.queue.put_nowait((message, status, "https://youtu.be/test", 123))
        with patch("app.bot.download", new=fake_download), patch("app.bot.STATUS_TIMEOUT", 0.01):
            worker = asyncio.create_task(service.worker())
            try:
                await asyncio.wait_for(service.queue.join(), 1)
                message.answer_video.assert_awaited_once()
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
