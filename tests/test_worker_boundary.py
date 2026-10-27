"""Untrusted downloader transport: no symlinks, special files, secrets or shared uploads."""
import asyncio
from contextlib import suppress
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.download_client import clear_requests, copy_result, download as remote_download
from app.download_worker import serve, serve_job
from app.downloader import DownloadError, TooLarge, child_environment
from app.job_protocol import JSON_LIMIT, read_json, write_json

JOB = "a" * 32


class ProtocolTests(unittest.TestCase):
    def test_response_reader_rejects_symlinks_special_files_and_unbounded_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "secret"
            secret.write_text("do not read")
            for kind in ["symlink", "fifo", "directory", "large", "invalid", "list", "encoding"]:
                path = root / "result.json"
                if kind == "symlink":
                    path.symlink_to(secret)
                elif kind == "fifo":
                    os.mkfifo(path)
                elif kind == "directory":
                    path.mkdir()
                elif kind == "large":
                    path.write_bytes(b"x" * (JSON_LIMIT + 1))
                elif kind == "invalid":
                    path.write_text("{bad")
                elif kind == "encoding":
                    path.write_bytes(b"\xff")
                else:
                    path.write_text("[]")
                with self.subTest(kind=kind), self.assertRaises((DownloadError, OSError)):
                    read_json(root, path.name)
                if kind == "directory":
                    path.rmdir()
                else:
                    path.unlink()

    def test_response_growth_after_stat_remains_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root, "result.json", {"ok": True})
            with patch("app.job_protocol.os.read", return_value=b"x" * (JSON_LIMIT + 1)):
                with self.assertRaises(DownloadError):
                    read_json(root, "result.json")

    def test_clear_requests_only_removes_protocol_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in [JOB + ".json", JOB + ".json.tmp", "keep.txt"]:
                (root / name).write_text("data")
            clear_requests(root)
            self.assertEqual([p.name for p in root.iterdir()], ["keep.txt"])

    def test_child_environment_contains_only_explicit_runtime_settings(self):
        with patch.dict("os.environ", {"TG_BOT_TOKEN": "fake-token", "TELEGRAM_API_HASH": "fake-hash",
                                      "GH_TOKEN": "fake-github", "PYTHONPATH": "/untrusted",
                                      "SSL_CERT_FILE": "/ca.pem", "SSL_CERT_DIR": "/certs"}, clear=True):
            env = child_environment()
        self.assertEqual(env["SSL_CERT_FILE"], "/ca.pem")
        self.assertEqual(env["SSL_CERT_DIR"], "/certs")
        self.assertEqual(set(env) & {"TG_BOT_TOKEN", "TELEGRAM_API_HASH", "GH_TOKEN", "PYTHONPATH"}, set())


class CopyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.root = Path(self.storage.name)
        self.outputs, self.private = self.root / "outputs", self.root / "private"
        self.outputs.mkdir()
        self.private.mkdir()
        self.job = self.outputs / ("video-saver-" + JOB)
        self.job.mkdir()
        self.disk = patch("app.download_client.shutil.disk_usage",
                          return_value=SimpleNamespace(free=100_000_000_000))
        self.disk.start()
        self.addCleanup(self.disk.stop)

    async def test_private_copy_cannot_be_modified_by_downloader_afterward(self):
        source = self.job / "video.mp4"
        source.write_bytes(b"original")
        target = await copy_result(self.outputs, JOB, "video.mp4", self.private, 100)
        self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)
        source.write_bytes(b"replaced")
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    async def test_path_swap_after_open_cannot_redirect_copy_to_private_file(self):
        import app.download_client as client
        source = self.job / "video.mp4"
        source.write_bytes(b"video")
        secret = self.private / "secret"
        secret.write_bytes(b"never expose")
        original_io = client.file_io
        swapped = False

        async def swap(function, *args):
            nonlocal swapped
            if function is os.read and not swapped:
                swapped = True
                source.unlink()
                source.symlink_to(secret)
            return await original_io(function, *args)

        with patch("app.download_client.file_io", new=swap):
            target = await copy_result(self.outputs, JOB, "video.mp4", self.private, 100)
        self.assertEqual(target.read_bytes(), b"video")

    async def test_symlinked_job_directory_is_rejected(self):
        self.job.rmdir()
        self.job.symlink_to(self.private, target_is_directory=True)
        (self.private / "video.mp4").write_bytes(b"private")
        with self.assertRaises(OSError):
            await copy_result(self.outputs, JOB, "video.mp4", self.private, 100)

    async def test_special_empty_oversized_and_symlinked_sources_are_rejected(self):
        source = self.job / "video.mp4"
        secret = self.private / "secret"
        secret.write_bytes(b"private")
        for kind in ["fifo", "directory", "empty", "oversize", "symlink"]:
            if kind == "fifo":
                os.mkfifo(source)
            elif kind == "directory":
                source.mkdir()
            elif kind == "empty":
                source.touch()
            elif kind == "oversize":
                source.write_bytes(b"x" * 101)
            else:
                source.symlink_to(secret)
            with self.subTest(kind=kind), self.assertRaises((DownloadError, OSError)):
                await asyncio.wait_for(copy_result(self.outputs, JOB, "video.mp4", self.private, 100), 1)
            self.assertFalse((self.private / "video.mp4").exists())
            source.rmdir() if kind == "directory" else source.unlink()

    async def test_invalid_result_name_and_existing_private_file_are_protected(self):
        for filename in ["../secret", "/etc/passwd", None, []]:
            with self.subTest(filename=filename), self.assertRaises(DownloadError):
                await copy_result(self.outputs, JOB, filename, self.private, 100)
        (self.job / "video.mp4").write_bytes(b"worker")
        existing = self.private / "video.mp4"
        existing.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            await copy_result(self.outputs, JOB, "video.mp4", self.private, 100)
        self.assertEqual(existing.read_bytes(), b"keep")

    async def test_low_disk_before_and_during_copy_is_rejected(self):
        (self.job / "video.mp4").write_bytes(b"video")
        for samples in [[SimpleNamespace(free=0)], [SimpleNamespace(free=100_000_000_000), SimpleNamespace(free=0)]]:
            with patch("app.download_client.shutil.disk_usage", side_effect=samples):
                with self.assertRaises(DownloadError):
                    await copy_result(self.outputs, JOB, "video.mp4", self.private, 100)
            self.assertFalse((self.private / "video.mp4").exists())


    async def test_cancellation_waits_for_inflight_disk_read_before_closing_fd(self):
        import threading
        source = self.job / "video.mp4"
        source.write_bytes(b"video")
        entered, release = threading.Event(), threading.Event()
        original_read = os.read
        opened = []

        def blocked_read(fd, size):
            opened.append(fd)
            entered.set()
            release.wait(2)
            return original_read(fd, size)

        with patch("app.download_client.os.read", side_effect=blocked_read):
            task = asyncio.create_task(copy_result(self.outputs, JOB, "video.mp4", self.private, 100))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                os.fstat(opened[0])  # fd must not be closed/reused while a thread owns it.
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        self.assertFalse((self.private / "video.mp4").exists())

    async def test_source_growth_and_truncation_cannot_bypass_copy_bounds(self):
        (self.job / "video.mp4").write_bytes(b"video")
        for data, exception in [(b"x" * 101, TooLarge), (b"", DownloadError)]:
            with patch("app.download_client.os.read", return_value=data):
                with self.assertRaises(exception):
                    await copy_result(self.outputs, JOB, "video.mp4", self.private, 100)
            self.assertFalse((self.private / "video.mp4").exists())


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.root = Path(self.storage.name)
        self.requests, self.results = self.root / "requests", self.root / "results"
        self.outputs, self.private = self.root / "outputs", self.root / "private"
        for root in [self.requests, self.results, self.outputs, self.private]:
            root.mkdir()
        for name, value in [
            ("app.download_worker.heartbeat", lambda: None),
            ("app.download_client.shutil.disk_usage", lambda path: SimpleNamespace(free=100_000_000_000)),
        ]:
            patcher = patch(name, new=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def run_client(self):
        return await remote_download("https://youtu.be/test", self.private, limit=100, timeout=3,
                                     request_dir=self.requests, result_dir=self.results, output_root=self.outputs)

    async def test_two_jobs_round_trip_without_shared_writable_uploads(self):
        async def fake_download(url, root, **kwargs):
            path = root / "video.mp4"
            path.write_bytes(b"video")
            return path

        with patch("app.download_worker.download", new=fake_download):
            worker = asyncio.create_task(serve(self.requests, self.results, self.outputs))
            try:
                for _ in range(2):
                    target = await self.run_client()
                    self.assertEqual(target.read_bytes(), b"video")
                    self.assertEqual(list(self.requests.iterdir()), [])
                    target.unlink()
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
        self.assertEqual(list(self.results.iterdir()), [])
        self.assertEqual(list(self.outputs.glob("video-saver-*")), [])

    async def test_downloader_errors_are_reported_and_cleaned(self):
        for error, expected in [(TooLarge(), TooLarge), (TimeoutError(), TimeoutError),
                                (DownloadError("unavailable"), DownloadError),
                                (RuntimeError("unexpected"), DownloadError), (OSError("disk"), DownloadError)]:
            async def fail(url, root, **kwargs):
                raise error
            with self.subTest(error=type(error)), patch("app.download_worker.download", new=fail):
                worker = asyncio.create_task(serve(self.requests, self.results, self.outputs))
                try:
                    with self.assertRaises(expected):
                        await self.run_client()
                finally:
                    worker.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker
            self.assertEqual(list(self.requests.iterdir()), [])

    async def test_client_cancellation_cancels_download_and_releases_files(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def blocked(url, root, **kwargs):
            (root / "video.part").write_bytes(b"partial")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with patch("app.download_worker.download", new=blocked):
            worker = asyncio.create_task(serve(self.requests, self.results, self.outputs))
            client = asyncio.create_task(self.run_client())
            try:
                await asyncio.wait_for(started.wait(), 1)
                client.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await client
                await asyncio.wait_for(stopped.wait(), 1)
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
        self.assertFalse(list(self.outputs.glob("video-saver-*")))

    async def test_invalid_requests_never_spawn_downloads(self):
        for data, error in [
            ({"url": "https://127.0.0.1"}, "download_failed"),
            ({"url": "https://youtu.be/test", "limit": True, "expires_at": time.time() + 10}, "download_failed"),
            ({"url": "https://youtu.be/test", "limit": 100, "expires_at": "invalid"}, "download_failed"),
            ({"url": "https://youtu.be/test", "limit": 100, "expires_at": 0}, "timeout"),
        ]:
            name = JOB + ".json"
            write_json(self.requests, name, data)
            with patch("app.download_worker.download") as download:
                task = asyncio.create_task(serve_job(self.requests, self.results, self.outputs, name))
                try:
                    async def response():
                        while not (self.results / name).exists():
                            await asyncio.sleep(0.01)
                        return read_json(self.results, name)
                    reply = await asyncio.wait_for(response(), 1)
                    self.assertEqual(reply["error"], error)
                    download.assert_not_called()
                finally:
                    (self.requests / name).unlink(missing_ok=True)
                    await asyncio.wait_for(task, 1)
