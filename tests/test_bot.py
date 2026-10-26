import asyncio
import runpy
from pathlib import Path
from contextlib import suppress
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessage

from app.bot import Config, VideoBot, main
from app.downloader import DownloadError, TooLarge


class ConfigTests(unittest.TestCase):
    def load(self, **updates):
        env = {"TG_BOT_TOKEN": "123:fake", "TG_ALLOWED_USER_IDS": "123, 456,123"}
        env.update(updates)
        with patch.dict("os.environ", env, clear=True):
            return Config.load()

    def test_valid_config_and_defaults(self):
        config = self.load()
        self.assertEqual(config.token, "123:fake")
        self.assertEqual(config.users, frozenset({123, 456}))
        self.assertEqual(config.limit, 50_000_000)
        self.assertEqual(config.timeout, 600)
        self.assertEqual(config.metube_url, "http://localhost:8081")

    def test_custom_limits_and_url(self):
        config = self.load(MAX_UPLOAD_BYTES="1234", DOWNLOAD_TIMEOUT="20", METUBE_URL="http://lan:8081")
        self.assertEqual((config.limit, config.timeout, config.metube_url), (1234, 20, "http://lan:8081"))

    def test_invalid_config_fails_closed(self):
        for updates in [
            {"TG_BOT_TOKEN": ""}, {"TG_BOT_TOKEN": "  "},
            {"TG_ALLOWED_USER_IDS": ""}, {"TG_ALLOWED_USER_IDS": "0"},
            {"TG_ALLOWED_USER_IDS": "-1"}, {"TG_ALLOWED_USER_IDS": "abc"},
            {"MAX_UPLOAD_BYTES": "abc"}, {"MAX_UPLOAD_BYTES": "0"},
            {"MAX_UPLOAD_BYTES": "50000001"}, {"DOWNLOAD_TIMEOUT": "0"},
            {"DOWNLOAD_TIMEOUT": "1801"}, {"DOWNLOAD_TIMEOUT": "oops"},
        ]:
            with self.subTest(updates=updates), self.assertRaises(SystemExit):
                self.load(**updates)


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = VideoBot(Config("123:example", frozenset({123, 456}), "http://metube.local:8081", 50, 600))

    def message(self, user_id=123, chat_type="private", text="https://youtu.be/test"):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            chat=SimpleNamespace(type=chat_type),
            text=text, sender_chat=None, reply_to_message=None,
            answer=AsyncMock(), answer_video=AsyncMock(), answer_document=AsyncMock(),
        )

    async def test_unknown_user_cannot_queue(self):
        message = self.message(user_id=789)
        await self.service.receive(message)
        self.assertTrue(self.service.queue.empty())
        message.answer.assert_not_awaited()

    async def test_groups_and_missing_sender_cannot_queue(self):
        for message in [self.message(chat_type="group"), self.message()]:
            if message.chat.type == "private":
                message.from_user = None
            await self.service.receive(message)
            message.answer.assert_not_awaited()
        self.assertTrue(self.service.queue.empty())

    async def test_identify_only_in_private_chat_with_sender(self):
        message = self.message(user_id=789)
        await self.service.identify(message)
        message.answer.assert_awaited_once_with("Твой Telegram ID: 789")
        message = self.message(chat_type="group")
        await self.service.identify(message)
        message.from_user = None
        message.chat.type = "private"
        await self.service.identify(message)
        message.answer.assert_not_awaited()

    async def test_help_and_metube_require_access(self):
        for handler in [self.service.help, self.service.metube]:
            allowed = self.message()
            await handler(allowed)
            allowed.answer.assert_awaited_once()
            denied = self.message(user_id=789)
            await handler(denied)
            denied.answer.assert_not_awaited()
        message = self.message()
        await self.service.metube(message)
        self.assertIn(self.service.config.metube_url, message.answer.call_args.args[0])

    async def test_invalid_input_never_queued(self):
        for text in ["no URL", "https://example.com/video", None]:
            message = self.message(text=text)
            await self.service.receive(message)
            message.answer.assert_awaited_once()
        self.assertTrue(self.service.queue.empty())
        self.assertFalse(self.service.pending)

    async def test_one_pending_per_user(self):
        message = self.message()
        await self.service.receive(message)
        await self.service.receive(message)
        self.assertEqual(self.service.queue.qsize(), 1)
        self.assertEqual(self.service.pending, {123})

    async def test_full_queue_rejected(self):
        self.service.queue = asyncio.Queue(maxsize=1)
        await self.service.receive(self.message())
        second = self.message(user_id=456)
        await self.service.receive(second)
        self.assertIn("Очередь заполнена", second.answer.call_args.args[0])
        self.assertEqual(self.service.pending, {123})

    async def test_concurrent_same_user_only_one_job(self):
        entered, release = asyncio.Event(), asyncio.Event()
        first, second = self.message(), self.message()
        status = SimpleNamespace(edit_text=AsyncMock())

        async def delayed_answer(*args, **kwargs):
            entered.set()
            await release.wait()
            return status

        first.answer.side_effect = delayed_answer
        task = asyncio.create_task(self.service.receive(first))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await self.service.receive(second)
            release.set()
            await task
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.assertEqual(self.service.queue.qsize(), 1)
        status.edit_text.assert_awaited_once()

    async def test_queue_fills_while_acknowledging(self):
        self.service.queue = asyncio.Queue(maxsize=1)
        entered, release = asyncio.Event(), asyncio.Event()
        first, second = self.message(), self.message(user_id=456)
        status = SimpleNamespace(edit_text=AsyncMock())

        async def delayed_answer(*args, **kwargs):
            entered.set()
            await release.wait()
            return status

        first.answer.side_effect = delayed_answer
        task = asyncio.create_task(self.service.receive(first))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await self.service.receive(second)
            release.set()
            await task
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.assertEqual(self.service.pending, {456})
        self.assertEqual(self.service.queue.qsize(), 1)
        status.edit_text.assert_awaited_once()

    async def run_worker(self, side_effect, *, message=None, status=None):
        message = message or self.message()
        status = status or SimpleNamespace(edit_text=AsyncMock())
        user_id = message.from_user.id
        self.service.pending.add(user_id)
        self.service.queue.put_nowait((message, status, message.text, user_id))
        with patch("app.bot.download", side_effect=side_effect):
            task = asyncio.create_task(self.service.worker())
            try:
                await asyncio.wait_for(self.service.queue.join(), timeout=3)
                self.assertFalse(task.done(), "Worker must survive a task failure")
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self.assertFalse(self.service.pending)
        return message, status

    async def test_download_errors_do_not_upload(self):
        for error, expected in [
            (TooLarge(), "metube.local"),
            (TimeoutError(), "10 минут"),
            (DownloadError("Видео недоступно"), "Видео недоступно"),
        ]:
            with self.subTest(error=type(error)):
                message, status = await self.run_worker(error)
                message.answer_video.assert_not_awaited()
                message.answer_document.assert_not_awaited()
                self.assertIn(expected, status.edit_text.call_args.args[0])

    async def test_upload_and_cleanup_for_video_and_document(self):
        for suffix in [".mp4", ".webm"]:
            paths = []

            async def fake_download(url, root, **kwargs):
                path = root / ("video" + suffix)
                path.write_bytes(b"video")
                paths.append(path)
                return path

            message, status = await self.run_worker(fake_download)
            if suffix == ".mp4":
                message.answer_video.assert_awaited_once()
                message.answer_document.assert_not_awaited()
            else:
                message.answer_document.assert_awaited_once()
                message.answer_video.assert_not_awaited()
            self.assertFalse(paths[0].parent.exists())
            self.assertEqual(status.edit_text.call_args.args[0], "Готово.")

    async def test_final_size_rechecked_before_upload(self):
        paths = []

        async def fake_download(url, root, **kwargs):
            path = root / "video.mp4"
            path.write_bytes(b"x" * 51)
            paths.append(path)
            return path

        message, status = await self.run_worker(fake_download)
        message.answer_video.assert_not_awaited()
        message.answer_document.assert_not_awaited()
        self.assertIn("больше 50 МБ", status.edit_text.call_args.args[0])
        self.assertFalse(paths[0].parent.exists())

    async def test_failed_upload_cleans_files_and_worker_survives(self):
        paths = []
        message = self.message()
        message.answer_video.side_effect = TelegramAPIError(
            method=SendMessage(chat_id=123, text="test"), message="upload refused"
        )

        async def fake_download(url, root, **kwargs):
            path = root / "video.mp4"
            path.write_bytes(b"video")
            paths.append(path)
            return path

        with self.assertLogs("app.bot", level="WARNING"):
            _, status = await self.run_worker(fake_download, message=message)
        self.assertFalse(paths[0].parent.exists())
        self.assertIn("Не удалось", status.edit_text.call_args.args[0])

    async def test_failed_error_notification_does_not_kill_worker(self):
        status = SimpleNamespace(edit_text=AsyncMock(side_effect=TelegramAPIError(
            method=SendMessage(chat_id=123, text="test"), message="chat inaccessible"
        )))
        with self.assertLogs("app.bot", level="WARNING"):
            await self.run_worker(RuntimeError("failure"), status=status)

    async def test_cancellation_cleans_files_and_releases_pending(self):
        entered = asyncio.Event()
        roots = []

        async def blocked_download(url, root, **kwargs):
            (root / "video.part").write_bytes(b"partial")
            roots.append(root)
            entered.set()
            await asyncio.Event().wait()

        message = self.message()
        self.service.pending.add(123)
        self.service.queue.put_nowait((message, SimpleNamespace(edit_text=AsyncMock()), message.text, 123))
        with patch("app.bot.download", side_effect=blocked_download):
            task = asyncio.create_task(self.service.worker())
            try:
                await asyncio.wait_for(entered.wait(), 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        await asyncio.wait_for(self.service.queue.join(), 2)
        self.assertFalse(self.service.pending)
        self.assertFalse(roots[0].exists())

    async def test_run_stops_worker_and_closes_session_after_polling(self):
        for error in [None, RuntimeError("polling failed")]:
            bot = MagicMock()
            bot.__aenter__ = AsyncMock(return_value=bot)
            bot.__aexit__ = AsyncMock(return_value=False)
            entered, cancelled = asyncio.Event(), asyncio.Event()

            async def worker():
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            async def polling(*args, **kwargs):
                await asyncio.wait_for(entered.wait(), 2)
                if error:
                    raise error

            with patch("app.bot.Bot", return_value=bot), patch("app.bot.AiohttpSession"), \
                    patch.object(self.service, "worker", side_effect=worker), \
                    patch.object(self.service.dp, "start_polling", side_effect=polling):
                if error:
                    with self.assertRaisesRegex(RuntimeError, "polling failed"):
                        await self.service.run()
                else:
                    await self.service.run()
            self.assertTrue(cancelled.is_set())
            bot.__aexit__.assert_awaited_once()


class EntryPointTests(unittest.TestCase):
    def test_main_runs_configured_bot(self):
        config = MagicMock()
        service = MagicMock()
        service.run.return_value = "coroutine-placeholder"
        with patch("app.bot.Config.load", return_value=config), \
                patch("app.bot.VideoBot", return_value=service) as constructor, \
                patch("app.bot.asyncio.run") as runner, patch("app.bot.logging.basicConfig"):
            main()
        constructor.assert_called_once_with(config)
        runner.assert_called_once_with("coroutine-placeholder")

    def test_script_entry_point(self):
        import app.bot

        def close_coroutine(coroutine):
            coroutine.close()

        with patch.dict("os.environ", {"TG_BOT_TOKEN": "123:fake", "TG_ALLOWED_USER_IDS": "123"}, clear=True), \
                patch("asyncio.run", side_effect=close_coroutine) as runner, patch("logging.basicConfig"):
            runpy.run_path(str(Path(app.bot.__file__)), run_name="__main__")
        runner.assert_called_once()
