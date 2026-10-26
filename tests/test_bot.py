import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.bot import Config, VideoBot
from app.downloader import TooLarge


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = VideoBot(Config("123:example", frozenset({123}), "http://metube.local:8081", 50, 600))

    def message(self, user_id=123, chat_type="private"):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            chat=SimpleNamespace(type=chat_type),
            text="https://youtu.be/test",
            answer=AsyncMock(), answer_video=AsyncMock(), answer_document=AsyncMock(),
        )

    async def test_unknown_user_cannot_queue(self):
        message = self.message(user_id=456)
        await self.service.receive(message)
        self.assertTrue(self.service.queue.empty())
        message.answer.assert_not_awaited()

    async def test_groups_cannot_queue(self):
        await self.service.receive(self.message(chat_type="group"))
        self.assertTrue(self.service.queue.empty())

    async def test_one_pending_per_user(self):
        message = self.message()
        await self.service.receive(message)
        await self.service.receive(message)
        self.assertEqual(self.service.queue.qsize(), 1)

    async def run_worker(self, side_effect):
        message = self.message()
        status = SimpleNamespace(edit_text=AsyncMock())
        self.service.pending.add(123)
        self.service.queue.put_nowait((message, status, message.text, 123))
        with patch("app.bot.download", side_effect=side_effect):
            task = asyncio.create_task(self.service.worker())
            await asyncio.wait_for(self.service.queue.join(), timeout=3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.service.pending)
        return message, status

    async def test_oversize_does_not_upload(self):
        message, status = await self.run_worker(TooLarge())
        message.answer_video.assert_not_awaited()
        message.answer_document.assert_not_awaited()
        self.assertIn("metube.local", status.edit_text.call_args.args[0])

    async def test_upload_and_cleanup(self):
        paths = []

        async def fake_download(url, root, **kwargs):
            path = root / "video.mp4"
            path.write_bytes(b"video")
            paths.append(path)
            return path

        message, _ = await self.run_worker(fake_download)
        message.answer_video.assert_awaited_once()
        self.assertFalse(paths[0].exists())

    async def test_config_fails_closed(self):
        with patch.dict("os.environ", {"TG_BOT_TOKEN": "123:fake", "TG_ALLOWED_USER_IDS": ""}, clear=True):
            with self.assertRaises(SystemExit):
                Config.load()

    async def test_upload_limit_cannot_exceed_50mb(self):
        with patch.dict("os.environ", {
            "TG_BOT_TOKEN": "123:fake", "TG_ALLOWED_USER_IDS": "123", "MAX_UPLOAD_BYTES": "50000001"
        }, clear=True):
            with self.assertRaises(SystemExit):
                Config.load()
