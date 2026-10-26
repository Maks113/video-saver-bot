import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.downloader import DownloadError, TooLarge, checked_file, download, validate_url


class Links(unittest.TestCase):
    def test_supported_sites(self):
        for url, site in [
            ("https://www.youtube.com/watch?v=abc", "youtube"),
            ("https://youtu.be/abc", "youtube"),
            ("https://www.instagram.com/reel/abc/", "instagram"),
            ("https://vkvideo.ru/video-1_2", "vk"),
            ("https://vk.com/clip-1_2", "vk"),
            ("https://vm.tiktok.com/abc/", "tiktok"),
        ]:
            self.assertEqual(validate_url(url), (url, site))

    def test_rejects_untrusted_urls(self):
        for text in [
            "https://youtube.com.attacker.test/a", "https://127.0.0.1/",
            "https://user:pass@youtube.com/a", "https://youtube.com:8080/a",
            "https://youtube.com:bad/a", "https://youtu.be/a https://youtu.be/b",
            "file:///etc/passwd", "not a URL",
        ]:
            with self.subTest(text=text), self.assertRaises(DownloadError):
                validate_url(text)

    def test_final_size_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "video.mp4"
            video.write_bytes(b"12345")
            self.assertEqual(checked_file(video, root, 5), video)
            with self.assertRaises(TooLarge):
                checked_file(video, root, 4)
            with self.assertRaises(DownloadError):
                checked_file(video, root / "other", 10)


class Subprocesses(unittest.IsolatedAsyncioTestCase):
    async def execute(self, script, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("app.downloader.make_command", return_value=[sys.executable, "-c", script, str(root)]):
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

    async def test_size_guard_kills_download(self):
        with patch("app.downloader.MAX_WORK_BYTES", 4):
            with self.assertRaises(TooLarge):
                await self.execute(
                    "import pathlib,sys,time; (pathlib.Path(sys.argv[1])/'video.part').write_bytes(b'12345'); time.sleep(10)"
                )

    async def test_timeout_kills_process(self):
        with self.assertRaises(TimeoutError):
            await self.execute("import time; time.sleep(10)", timeout=0.1)
