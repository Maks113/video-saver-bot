"""Group commands through the real aiogram dispatcher, with Telegram calls stubbed."""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.methods import SendMessage, SendVideo
from aiogram.types import Message, Update, User

from app.bot import Config, VideoBot


class GroupTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = VideoBot(Config("123:fake", frozenset({123}), "http://metube", 50, 600))
        self.bot = Bot("123:fake")
        self.bot._me = User(id=999, is_bot=True, first_name="Saver", username="video_test_bot")

    async def asyncTearDown(self):
        await self.bot.session.close()

    def message(self, text, *, chat_type="supergroup", user_id=123, reply=None, sender_chat=None, topic=None):
        group = chat_type in {"group", "supergroup", "channel"}
        data = {
            "message_id": 10,
            "date": datetime.now(timezone.utc),
            "chat": {"id": -100123 if group else 123, "type": chat_type, "title": "Test"},
            "from": {"id": user_id, "is_bot": False, "first_name": "User"},
            "text": text,
            "reply_to_message": reply,
            "sender_chat": sender_chat,
            "is_topic_message": bool(topic),
            "message_thread_id": topic,
        }
        return Message.model_validate(data).as_(self.bot)

    async def dispatch(self, message):
        status = Message.model_validate({
            "message_id": 11, "date": message.date, "chat": message.chat,
            "is_topic_message": message.is_topic_message, "message_thread_id": message.message_thread_id,
        }).as_(self.bot)
        api = AsyncMock(return_value=status)
        with patch.object(Bot, "__call__", new=api):
            await self.service.dp.feed_update(self.bot, Update(update_id=1, message=message))
        return api

    async def test_addressed_command_queues_in_group_and_supergroup(self):
        for chat_type in ["group", "supergroup"]:
            self.service = VideoBot(self.service.config)
            message = self.message("/download@video_test_bot https://youtu.be/test", chat_type=chat_type)
            api = await self.dispatch(message)
            self.assertEqual(self.service.queue.qsize(), 1)
            queued, _, url, user_id = self.service.queue.get_nowait()
            self.assertEqual(queued.chat.id, -100123)
            self.assertEqual(url, "https://youtu.be/test")
            self.assertEqual(user_id, 123)
            api.assert_awaited_once()

    async def test_plain_command_and_private_chat_supported(self):
        for chat_type in ["private", "group"]:
            self.service = VideoBot(self.service.config)
            await self.dispatch(self.message("/download https://youtu.be/test", chat_type=chat_type))
            self.assertEqual(self.service.queue.qsize(), 1)

    async def test_command_addressed_to_another_bot_is_ignored(self):
        api = await self.dispatch(self.message("/download@other_bot https://youtu.be/test"))
        self.assertTrue(self.service.queue.empty())
        api.assert_not_awaited()

    async def test_group_links_and_other_commands_are_ignored(self):
        for text in ["https://youtu.be/test", "hello", "/unknown https://youtu.be/test"]:
            api = await self.dispatch(self.message(text))
            api.assert_not_awaited()
        self.assertTrue(self.service.queue.empty())

    async def test_unknown_user_and_anonymous_sender_cannot_download(self):
        for message in [
            self.message("/download https://youtu.be/test", user_id=456),
            self.message("/download https://youtu.be/test",
                         sender_chat={"id": -100123, "type": "supergroup", "title": "Anonymous"}),
            self.message("/download https://youtu.be/test", chat_type="channel"),
        ]:
            api = await self.dispatch(message)
            api.assert_not_awaited()
        self.assertTrue(self.service.queue.empty())

    async def test_reply_uses_text_or_caption_and_requesters_identity(self):
        for field in ["text", "caption"]:
            self.service = VideoBot(self.service.config)
            reply = {
                "message_id": 9, "date": datetime.now(timezone.utc),
                "chat": {"id": -100123, "type": "supergroup", "title": "Test"},
                "from": {"id": 456, "is_bot": False, "first_name": "Other"},
                field: "https://youtu.be/replied",
            }
            await self.dispatch(self.message("/download", reply=reply))
            _, _, url, user_id = self.service.queue.get_nowait()
            self.assertEqual((url, user_id), ("https://youtu.be/replied", 123))

    async def test_explicit_argument_takes_precedence_over_reply(self):
        reply = self.message("https://youtu.be/old").model_dump(by_alias=True)
        await self.dispatch(self.message("/download https://youtu.be/new", reply=reply))
        self.assertEqual(self.service.queue.get_nowait()[2], "https://youtu.be/new")

    async def test_missing_or_invalid_argument_reports_error_without_queue(self):
        for message in [
            self.message("/download"),
            self.message("/download https://example.com/test"),
            self.message("/download", reply=self.message("no link").model_dump(by_alias=True)),
        ]:
            api = await self.dispatch(message)
            api.assert_awaited_once()
        self.assertTrue(self.service.queue.empty())

    async def test_user_has_one_pending_job_across_chats(self):
        await self.dispatch(self.message("https://youtu.be/private", chat_type="private"))
        api = await self.dispatch(self.message("/download https://youtu.be/group"))
        self.assertEqual(self.service.queue.qsize(), 1)
        self.assertIn("ещё обрабатывается", api.call_args.args[0].text)

    async def test_forum_status_and_video_stay_in_original_topic(self):
        message = self.message("/download https://youtu.be/test", topic=42)
        api = await self.dispatch(message)
        status_call = api.call_args.args[0]
        self.assertIsInstance(status_call, SendMessage)
        self.assertEqual((status_call.chat_id, status_call.message_thread_id), (-100123, 42))

        async def fake_download(url, root, **kwargs):
            path = root / "video.mp4"
            path.write_bytes(b"video")
            return path

        status = self.message("status", topic=42)
        worker_api = AsyncMock(return_value=status)
        with patch("app.bot.download", new=fake_download), patch.object(Bot, "__call__", new=worker_api):
            worker = asyncio.create_task(self.service.worker())
            try:
                await asyncio.wait_for(self.service.queue.join(), 3)
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
        videos = [call.args[0] for call in worker_api.await_args_list if isinstance(call.args[0], SendVideo)]
        self.assertEqual(len(videos), 1)
        self.assertEqual((videos[0].chat_id, videos[0].message_thread_id), (-100123, 42))
        self.assertFalse(self.service.pending)

    async def test_help_describes_group_command(self):
        api = await self.dispatch(self.message("/help"))
        self.assertIn("/download", api.call_args.args[0].text)
