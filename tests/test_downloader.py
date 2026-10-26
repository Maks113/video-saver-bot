import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from yt_dlp import parse_options

from app.downloader import (
    DownloadError, TooLarge, checked_file, download, make_command,
    stop_process, validate_url, work_size,
)


class Links(unittest.TestCase):
    def test_supported_sites(self):
        for url, site in [
            ("https://www.youtube.com/watch?v=abc", "youtube"),
            ("https://youtu.be/abc", "youtube"),
            ("https://www.instagram.com/reel/abc/", "instagram"),
            ("https://vkvideo.ru/video-1_2", "vk"),
            ("https://vk.ru/video-1_2", "vk"),
            ("https://vk.com/clip-1_2", "vk"),
            ("https://vm.tiktok.com/abc/", "tiktok"),
        ]:
            self.assertEqual(validate_url(url), (url, site))

    def test_link_embedded_in_message(self):
        self.assertEqual(validate_url("Скачай <https://YOUTU.BE/abc>."),
                         ("https://YOUTU.BE/abc", "youtube"))

    def test_rejects_untrusted_urls(self):
        for text in [
            "https://youtube.com.attacker.test/a", "https://127.0.0.1/",
            "https://user:pass@youtube.com/a", "https://youtube.com:8080/a",
            "https://youtube.com:bad/a", "https://youtu.be/a https://youtu.be/b",
            "https://[invalid/", "https://", "file:///etc/passwd", "not a URL",
        ]:
            with self.subTest(text=text), self.assertRaises(DownloadError):
                validate_url(text)


class Files(unittest.TestCase):
    def test_final_size_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "video.mp4"
            video.write_bytes(b"12345")
            self.assertEqual(checked_file(video, root, 5), video)
            with self.assertRaises(TooLarge):
                checked_file(video, root, 4)

    def test_rejects_missing_wrong_type_empty_and_outside_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            for name, data in [("empty.mp4", b""), ("cookies.txt", b"cookie")]:
                path = work / name
                path.write_bytes(data)
                with self.assertRaises(DownloadError):
                    checked_file(path, work, 100)
            with self.assertRaises(DownloadError):
                checked_file(work / "missing.mp4", work, 100)
            outside = root / "outside.mp4"
            outside.write_bytes(b"video")
            link = work / "video.mp4"
            link.symlink_to(outside)
            with self.assertRaises(DownloadError):
                checked_file(link, work, 100)

    def test_work_size_counts_partial_and_media_but_not_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "video.part").write_bytes(b"abc")
            (root / "audio.m4a").write_bytes(b"defgh")
            (root / "subdir").mkdir()
            self.assertEqual(work_size(root), 8)


class Commands(unittest.TestCase):
    def test_command_is_accepted_by_real_ytdlp_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cookies = root / "cookies"
            cookies.mkdir()
            with patch("app.downloader.Path", return_value=cookies):
                cmd = make_command("https://youtu.be/test", root, "youtube", 12345)
            self.assertEqual(cmd[:3], [sys.executable, "-m", "yt_dlp"])
            parsed = parse_options(cmd[3:])
            self.assertEqual(parsed.urls, ["https://youtu.be/test"])
            self.assertEqual(parsed.ydl_opts["max_filesize"], 12345)
            self.assertTrue(parsed.ydl_opts["noplaylist"])
            self.assertEqual(parsed.ydl_opts["playlist_items"], "1")
            self.assertNotIn("--cookies", cmd)
            # Validate the filter itself, including formats without is_live metadata.
            match_filter = parsed.ydl_opts["match_filter"]
            self.assertIsNotNone(match_filter({"is_live": True}, incomplete=False))
            self.assertIsNone(match_filter({"is_live": False}, incomplete=False))

    def test_only_site_cookie_is_copied_and_original_is_not_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cookies, work = root / "cookies", root / "work"
            cookies.mkdir()
            work.mkdir()
            source = cookies / "instagram.txt"
            source.write_text("# Netscape HTTP Cookie File\n")
            (cookies / "youtube.txt").write_text("other account")
            with patch("app.downloader.Path", return_value=cookies):
                cmd = make_command("https://instagram.com/reel/test", work, "instagram", 50000)
            target = Path(cmd[cmd.index("--cookies") + 1])
            self.assertEqual(target.parent, work)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            target.write_text("updated cookie jar")
            self.assertEqual(source.read_text(), "# Netscape HTTP Cookie File\n")


class StopProcesses(unittest.IsolatedAsyncioTestCase):
    async def test_already_exited_leader_still_triggers_group_cleanup(self):
        proc = SimpleNamespace(returncode=0, pid=123, wait=AsyncMock())
        with patch("app.downloader.os.killpg") as kill:
            await stop_process(proc)
        kill.assert_called_once_with(123, signal.SIGKILL)
        proc.wait.assert_awaited_once()

    async def test_process_exits_between_check_and_kill(self):
        proc = SimpleNamespace(returncode=None, pid=123, wait=AsyncMock())
        with patch("app.downloader.os.killpg", side_effect=ProcessLookupError) as kill:
            await stop_process(proc)
        kill.assert_called_once_with(123, signal.SIGKILL)
        proc.wait.assert_awaited_once()


class Subprocesses(unittest.IsolatedAsyncioTestCase):
    async def execute(self, script, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("app.downloader.make_command", side_effect=lambda url, directory, *args, **kwargs:
                       [sys.executable, "-c", script, str(directory)]):
                return await download("https://youtu.be/test", root, **kwargs)

    async def test_successful_download(self):
        await self.execute(
            "import pathlib,sys,json; p=pathlib.Path(sys.argv[1])/'video.mp4'; "
            "p.write_bytes(b'video'); print(json.dumps(str(p)))"
        )

    async def test_rejects_oversized_final_file(self):
        with self.assertRaises(TooLarge):
            await self.execute(
                "import pathlib,sys,json; p=pathlib.Path(sys.argv[1])/'video.mp4'; "
                "p.write_bytes(b'123456'); print(json.dumps(str(p)))", limit=5
            )

    async def test_handles_skipped_download(self):
        with self.assertRaises(DownloadError):
            await self.execute("pass")

    async def test_handles_login_error(self):
        with self.assertRaisesRegex(DownloadError, "авторизация"):
            await self.execute("import sys; sys.stderr.write('Please log in'); sys.exit(1)")

    async def test_handles_generic_failure(self):
        with self.assertRaisesRegex(DownloadError, "Не удалось скачать"):
            await self.execute("import sys; sys.stderr.write('unavailable'); sys.exit(1)")

    async def test_handles_explicit_size_rejection(self):
        for error in ["max-filesize exceeded", "larger than limit", "maximum file size exceeded"]:
            with self.subTest(error=error), self.assertRaises(TooLarge):
                await self.execute("import sys; sys.stderr.write(" + repr(error) + "); sys.exit(1)")

    async def test_handles_invalid_result(self):
        for output in ["not JSON", "null", "123"]:
            with self.subTest(output=output), self.assertRaisesRegex(DownloadError, "определить"):
                await self.execute("print(" + repr(output) + ")")

    async def test_size_guard_kills_download(self):
        with patch("app.downloader.MAX_WORK_BYTES", 4):
            with self.assertRaises(TooLarge):
                await self.execute(
                    "import pathlib,sys,time; (pathlib.Path(sys.argv[1])/'video.part').write_bytes(b'12345'); time.sleep(10)"
                )

    async def test_timeout_kills_process(self):
        with self.assertRaises(TimeoutError):
            await self.execute("import time; time.sleep(10)", timeout=0.1)

    async def test_cancel_kills_and_reaps_real_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = (
                "import os,pathlib,sys,time; "
                "(pathlib.Path(sys.argv[1])/'pid').write_text(str(os.getpid())); time.sleep(30)"
            )
            cmd = [sys.executable, "-c", script, str(root)]
            with patch("app.downloader.make_command", return_value=cmd):
                task = asyncio.create_task(download("https://youtu.be/test", root))

                async def started():
                    while True:
                        try:
                            value = (root / "pid").read_text()
                        except FileNotFoundError:
                            value = ""
                        if value:
                            return int(value)
                        await asyncio.sleep(0.01)

                try:
                    pid = await asyncio.wait_for(started(), 3)
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
