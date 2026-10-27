"""Fault injection at the boundary between Telegram logOut and durable local state."""
import asyncio
from contextlib import suppress
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, sentinel

from app.bot import Config
from app.migrate_api import migrate
from app.runtime import migration_state, require_local_ready, write_migration


class SimulatedCrash(BaseException):
    pass


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def bot(self, *, logout_error=None, local_error=None):
        bot = MagicMock()
        bot.__aenter__ = AsyncMock(return_value=bot)
        bot.__aexit__ = AsyncMock(return_value=False)
        bot.log_out = AsyncMock(return_value=True, side_effect=logout_error)
        bot.get_me = AsyncMock(return_value=SimpleNamespace(id=123), side_effect=local_error)
        return bot

    def config(self, root):
        return Config("123:fake", frozenset({123}), "http://metube", 2_000_000_000, 1800,
                      api_base="http://local-api", download_dir=root)

    def factory(self, cloud, local):
        return lambda token, session: cloud if session is sentinel.cloud else local

    async def test_lost_logout_response_is_not_retried_when_local_auth_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            cloud = self.bot(logout_error=TimeoutError("response was lost"))
            local = self.bot(local_error=ConnectionError("local API unavailable"))
            with patch("app.migrate_api.AiohttpSession", return_value=sentinel.cloud), \
                    patch("app.migrate_api.local_session", return_value=sentinel.local), \
                    patch("app.migrate_api.Bot", side_effect=self.factory(cloud, local)):
                for _ in range(2):
                    with self.assertRaises((TimeoutError, ConnectionError)):
                        await migrate(config)
            self.assertEqual(cloud.log_out.await_count, 1)
            self.assertEqual(local.get_me.await_count, 1)
            self.assertEqual(migration_state(root)["stage"], "logout_requested")
            with self.assertRaises(SystemExit):
                require_local_ready(root, config.token, config.api_base)

    async def test_crash_after_response_before_confirmation_write_resumes_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            cloud, local = self.bot(), self.bot()

            def crash_before_confirmation(root, token, base, stage):
                if stage == "cloud_logged_out":
                    raise SimulatedCrash()
                return write_migration(root, token, base, stage)

            with patch("app.migrate_api.AiohttpSession", return_value=sentinel.cloud), \
                    patch("app.migrate_api.local_session", return_value=sentinel.local), \
                    patch("app.migrate_api.Bot", side_effect=self.factory(cloud, local)):
                with patch("app.migrate_api.write_migration", side_effect=crash_before_confirmation):
                    with self.assertRaises(SimulatedCrash):
                        await migrate(config)
                await migrate(config)
            self.assertEqual(cloud.log_out.await_count, 1)
            require_local_ready(root, config.token, config.api_base)

    async def test_intent_is_durable_before_rpc_and_failed_sync_prevents_rpc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            cloud, local = self.bot(), self.bot()

            async def check_intent():
                self.assertEqual(migration_state(root)["stage"], "logout_requested")
                return True

            cloud.log_out.side_effect = check_intent
            with patch("app.migrate_api.AiohttpSession", return_value=sentinel.cloud), \
                    patch("app.migrate_api.local_session", return_value=sentinel.local), \
                    patch("app.migrate_api.Bot", side_effect=self.factory(cloud, local)):
                await migrate(config)
            self.assertEqual(migration_state(root)["stage"], "ready")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("app.runtime.os.fsync", side_effect=OSError("sync failed")), \
                    patch("app.migrate_api.Bot") as bot:
                with self.assertRaises(OSError):
                    await migrate(self.config(root))
                bot.assert_not_called()
            self.assertEqual(migration_state(root), {})

    async def test_uncertain_local_success_preserves_uncertainty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            write_migration(root, config.token, config.api_base, "logout_requested")
            cloud, local = self.bot(), self.bot()
            with patch("app.migrate_api.local_session", return_value=sentinel.local), \
                    patch("app.migrate_api.Bot", side_effect=self.factory(cloud, local)):
                await migrate(config)
                await migrate(config)
            cloud.log_out.assert_not_awaited()
            self.assertEqual(migration_state(root)["stage"], "ready_unconfirmed")
            require_local_ready(root, config.token, config.api_base)

    async def test_cancelled_logout_does_not_cause_automatic_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            cloud, local = self.bot(), self.bot()
            started = asyncio.Event()

            async def blocked():
                started.set()
                await asyncio.Event().wait()

            cloud.log_out.side_effect = blocked
            with patch("app.migrate_api.AiohttpSession", return_value=sentinel.cloud), \
                    patch("app.migrate_api.local_session", return_value=sentinel.local), \
                    patch("app.migrate_api.Bot", side_effect=self.factory(cloud, local)):
                task = asyncio.create_task(migrate(config))
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await migrate(config)
            self.assertEqual(cloud.log_out.await_count, 1)
            self.assertEqual(migration_state(root)["stage"], "ready_unconfirmed")

    async def test_explicit_rejection_requires_explicit_operator_retry(self):
        from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramServerError
        from aiogram.methods import LogOut
        for error, stage in [
            (TelegramBadRequest(method=LogOut(), message="rejected"), "logout_rejected"),
            (TelegramNetworkError(method=LogOut(), message="connection lost"), "logout_requested"),
            (TelegramServerError(method=LogOut(), message="server failed"), "logout_requested"),
        ]:
            with self.subTest(error=type(error)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = self.config(root)
                cloud, local = self.bot(logout_error=error), self.bot()
                with patch("app.migrate_api.AiohttpSession", return_value=sentinel.cloud), \
                        patch("app.migrate_api.local_session", return_value=sentinel.local), \
                        patch("app.migrate_api.Bot", side_effect=self.factory(cloud, local)):
                    with self.assertRaises(type(error)):
                        await migrate(config)
                    self.assertEqual(migration_state(root)["stage"], stage)
                    if stage == "logout_rejected":
                        with self.assertRaises(RuntimeError):
                            await migrate(config)
                    cloud.log_out.side_effect = None
                    await migrate(config, retry_cloud_logout=True)
                    self.assertEqual(cloud.log_out.await_count, 2)
                    await migrate(config, retry_cloud_logout=True)
                    self.assertEqual(cloud.log_out.await_count, 2)  # Confirmed ready never relogs cloud.
                self.assertEqual(migration_state(root)["stage"], "ready")

    async def test_failure_to_sync_directory_after_rename_never_sends_rpc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("app.runtime.os.fsync", side_effect=[None, OSError("directory sync failed")]), \
                    patch("app.migrate_api.Bot") as bot:
                with self.assertRaises(OSError):
                    await migrate(self.config(root))
                bot.assert_not_called()
            self.assertEqual(migration_state(root)["stage"], "logout_requested")
