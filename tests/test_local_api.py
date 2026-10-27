"""Local API transport, migration safety, persistent storage and format contracts."""
import asyncio
from contextlib import nullcontext, suppress
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from yt_dlp import YoutubeDL, parse_options

from app.bot import Config, VideoBot
from app.downloader import DownloadError, MAX_UPLOAD_BYTES, download, make_command
from app.migrate_api import migrate
from app.runtime import (
    MARKER, local_session, migration_state, require_local_ready, workspace, write_migration,
)


class FormatPolicy(unittest.TestCase):
    def select(self, formats):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.downloader.Path.is_file", return_value=False):
                command = make_command("https://youtu.be/test", Path(tmp), "youtube", MAX_UPLOAD_BYTES)
            options = parse_options(command[3:]).ydl_opts
            with YoutubeDL(options) as ydl:
                return ydl.process_ie_result({
                    "id": "fixture", "title": "Fixture", "extractor": "fixture", "formats": formats,
                }, download=False)

    def video(self, name, width, height, size, **overrides):
        result = {
            "format_id": name, "url": f"https://cdn.example/{name}.mp4",
            "width": width, "height": height, "filesize": size,
            "ext": "mp4", "vcodec": "h264", "acodec": "aac",
        }
        result.update(overrides)
        return result

    def test_portrait_720p_beats_lower_portrait_format(self):
        formats = [
            self.video("360", 360, 640, 10_000_000),
            self.video("720", 720, 1280, 20_000_000),
        ]
        self.assertEqual(self.select(formats)["format_id"], "720")

    def test_landscape_portrait_rotation_and_unknown_dimensions_are_accepted(self):
        for width, height, rotation in [(1280, 720, 0), (720, 1280, 0), (720, 1280, 90),
                                         (1080, 1920, 0), (None, None, 0)]:
            with self.subTest(width=width, height=height, rotation=rotation):
                selected = self.select([self.video("valid", width, height, 20_000_000, rotation=rotation)])
                self.assertEqual(selected["format_id"], "valid")

    def test_best_audio_no_longer_triggers_50mb_size_fallback(self):
        formats = [
            {"format_id": "audio-small", "url": "https://cdn.example/small.m4a",
             "ext": "m4a", "acodec": "aac", "vcodec": "none", "abr": 64, "filesize": 2_000_000},
            {"format_id": "audio-best", "url": "https://cdn.example/best.m4a",
             "ext": "m4a", "acodec": "aac", "vcodec": "none", "abr": 192, "filesize": 10_000_000},
            self.video("video", 1280, 720, 45_000_000, acodec="none"),
        ]
        selected = self.select(formats)
        self.assertEqual([f["format_id"] for f in selected["requested_formats"]], ["video", "audio-best"])
        self.assertEqual(sum(f["filesize"] for f in selected["requested_formats"]), 55_000_000)
        self.assertLess(55_000_000, MAX_UPLOAD_BYTES)


class WorkspaceTests(unittest.TestCase):
    def test_cleans_crashed_jobs_but_keeps_state_and_unrelated_data(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            old = root / "video-saver-crashed"
            old.mkdir()
            (old / "partial.mp4").write_bytes(b"partial")
            (root / "keep.txt").write_text("keep")
            (root / "video-saver-link").symlink_to(outside, target_is_directory=True)
            write_migration(root, "123:fake", "http://api", "ready")
            with workspace(root):
                self.assertFalse(old.exists())
                self.assertTrue((root / "keep.txt").exists())
                self.assertTrue((root / "video-saver-link").is_symlink())
                require_local_ready(root, "123:fake", "http://api")

    def test_second_worker_or_migration_cannot_clean_active_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with workspace(root):
                active = root / "video-saver-active"
                active.mkdir()
                with self.assertRaises(SystemExit), workspace(root):
                    pass
                self.assertTrue(active.exists())
            with workspace(root):
                self.assertFalse(active.exists())

    def test_root_required(self):
        with self.assertRaises(SystemExit), workspace(None):
            pass

    def test_missing_incomplete_mismatched_and_corrupt_markers_refuse_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(migration_state(root), {})
            with self.assertRaises(SystemExit):
                require_local_ready(root, "123:fake", "http://api")
            for stage in ["cloud_logged_out", "ready"]:
                write_migration(root, "456:other", "http://api", stage)
                with self.assertRaises(SystemExit):
                    require_local_ready(root, "123:fake", "http://api")
            for invalid in ["{bad", "[]"]:
                (root / MARKER).write_text(invalid)
                with self.assertRaises(SystemExit):
                    migration_state(root)

    def test_marker_contains_no_bot_token_and_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_migration(root, "123:secret", "http://api", "ready")
            marker = root / MARKER
            self.assertNotIn("secret", marker.read_text())
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            require_local_ready(root, "123:secret", "http://api")


class LocalTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_session_uses_local_file_mode(self):
        session = local_session("http://telegram-bot-api:8081", 7200)
        try:
            self.assertTrue(session.api.is_local)
            self.assertEqual(session.api.api_url("123:fake", "getMe"),
                             "http://telegram-bot-api:8081/bot123:fake/getMe")
            self.assertEqual(session.timeout, 7200)
        finally:
            await session.close()

    async def test_file_larger_than_50mb_sent_by_path_then_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config("123:fake", frozenset({123}), "http://metube", MAX_UPLOAD_BYTES, 1800,
                            download_dir=root)
            service = VideoBot(config)
            message = SimpleNamespace(answer_video=AsyncMock(), answer_document=AsyncMock())
            status = SimpleNamespace(edit_text=AsyncMock())
            paths = []

            async def fake_download(url, folder, **kwargs):
                self.assertEqual(folder.parent, root)
                path = folder / "video.mp4"
                with path.open("wb") as file:
                    file.truncate(60_000_000)  # Sparse fixture, no large memory allocation.
                paths.append(path)
                return path

            service.pending.add(123)
            service.queue.put_nowait((message, status, "https://youtu.be/test", 123))
            with patch("app.bot.download", new=fake_download):
                task = asyncio.create_task(service.worker())
                try:
                    await asyncio.wait_for(service.queue.join(), 2)
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            message.answer_video.assert_awaited_once_with(
                paths[0].as_uri(), supports_streaming=True, request_timeout=7200)
            self.assertEqual(list(root.iterdir()), [])

    async def test_low_disk_refuses_before_spawning_downloader(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.downloader.shutil.disk_usage", return_value=SimpleNamespace(free=0)), \
                    patch("app.downloader.asyncio.create_subprocess_exec") as spawn:
                with self.assertRaisesRegex(DownloadError, "недостаточно"):
                    await download("https://youtu.be/test", Path(tmp))
                spawn.assert_not_called()

    async def test_disk_reserve_exhausted_during_download_stops_process(self):
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.downloader.shutil.disk_usage",
                       side_effect=[SimpleNamespace(free=100_000_000_000), SimpleNamespace(free=0)]), \
                    patch("app.downloader.make_command",
                          return_value=[sys.executable, "-c", "import time; time.sleep(10)"]):
                with self.assertRaisesRegex(DownloadError, "недостаточно"):
                    await download("https://youtu.be/test", Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def bot(self, *, logout=True, local_error=None, bot_id=123):
        instance = MagicMock()
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instance.log_out = AsyncMock(return_value=logout)
        instance.get_me = AsyncMock(return_value=SimpleNamespace(id=bot_id), side_effect=local_error)
        return instance

    async def test_logout_precedes_local_auth_and_retry_never_repeats_cloud_logout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config("123:fake", frozenset({123}), "http://metube", MAX_UPLOAD_BYTES, 1800,
                            api_base="http://api", download_dir=root)
            cloud = self.bot()
            failed_local = self.bot(local_error=ConnectionError("unavailable"))

            async def check_order():
                self.assertIn(migration_state(root)["stage"], {"cloud_logged_out", "ready"})
                return SimpleNamespace(id=123)

            local = self.bot()
            local.get_me.side_effect = check_order
            with patch("app.migrate_api.Bot", side_effect=[cloud, failed_local]), \
                    patch("app.migrate_api.AiohttpSession"), patch("app.migrate_api.local_session"):
                with self.assertRaises(ConnectionError):
                    await migrate(config)
            cloud.log_out.assert_awaited_once()
            self.assertEqual(migration_state(root)["stage"], "cloud_logged_out")
            with self.assertRaises(SystemExit):
                require_local_ready(root, config.token, config.api_base)
            with patch("app.migrate_api.Bot", return_value=local) as constructor, \
                    patch("app.migrate_api.local_session"):
                await migrate(config)
                self.assertEqual(constructor.call_count, 1)
            require_local_ready(root, config.token, config.api_base)
            with patch("app.migrate_api.Bot", return_value=local) as constructor, \
                    patch("app.migrate_api.local_session"):
                await migrate(config)
                self.assertEqual(constructor.call_count, 1)

    async def test_failed_logout_wrong_identity_or_stale_marker_never_mark_ready(self):
        for reason in ["logout", "identity", "stale"]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = Config("123:fake", frozenset({123}), "http://metube", MAX_UPLOAD_BYTES, 1800,
                                api_base="http://api", download_dir=root)
                cloud = self.bot(logout=reason != "logout")
                local = self.bot(bot_id=456 if reason == "identity" else 123)
                if reason == "stale":
                    write_migration(root, "456:other", "http://other", "ready")
                with patch("app.migrate_api.Bot", side_effect=[cloud, local]), \
                        patch("app.migrate_api.AiohttpSession"), patch("app.migrate_api.local_session"):
                    with self.assertRaises(RuntimeError):
                        await migrate(config)
                with self.assertRaises(SystemExit):
                    require_local_ready(root, config.token, config.api_base)


class LocalConfigTests(unittest.TestCase):
    def config(self, **overrides):
        env = {"TG_BOT_TOKEN": "123:fake", "TG_ALLOWED_USER_IDS": "123"}
        env.update(overrides)
        with patch.dict("os.environ", env, clear=True):
            return Config.load()

    def test_local_defaults_and_explicit_transport_settings(self):
        config = self.config()
        self.assertEqual(config.api_base, "http://telegram-bot-api:8081")
        self.assertEqual(config.download_dir, Path("/uploads"))
        self.assertEqual(config.upload_timeout, 7200)
        config = self.config(BOT_API_BASE="http://api:8081/", DOWNLOAD_DIR="/data/shared",
                             UPLOAD_TIMEOUT="3600", MAX_UPLOAD_BYTES="1900000000")
        self.assertEqual(config.api_base, "http://api:8081")
        self.assertEqual(config.download_dir, Path("/data/shared"))
        self.assertEqual(config.limit, 1_900_000_000)
        self.assertEqual(config.upload_timeout, 3600)

    def test_invalid_urls_paths_and_upload_timeouts_are_refused(self):
        for update in [
            {"BOT_API_BASE": "ftp://api"}, {"BOT_API_BASE": "http:///"},
            {"BOT_API_BASE": "http://user:secret@api"}, {"BOT_API_BASE": "http://api/?key=x"},
            {"BOT_API_BASE": "http://api/#fragment"}, {"BOT_API_BASE": "http://[invalid"},
            {"BOT_API_BASE": "http://api:bad"}, {"BOT_API_BASE": "http://api:0"},
            {"DOWNLOAD_DIR": "relative"}, {"UPLOAD_TIMEOUT": "0"},
            {"UPLOAD_TIMEOUT": "14401"}, {"UPLOAD_TIMEOUT": "invalid"},
        ]:
            with self.subTest(update=update), self.assertRaises(SystemExit):
                self.config(**update)


class MigrationEntryTests(unittest.TestCase):
    def test_main_reports_failure_without_exposing_exception_secrets(self):
        import app.migrate_api as migration
        import io

        def failed(coroutine):
            coroutine.close()
            raise RuntimeError("secret-token-must-not-be-logged")

        output = io.StringIO()
        with patch("app.migrate_api.Config.load", return_value=MagicMock()), \
                patch("app.migrate_api.asyncio.run", side_effect=failed), \
                patch("sys.stderr", output):
            self.assertEqual(migration.main([]), 1)
        self.assertIn("RuntimeError", output.getvalue())
        self.assertNotIn("secret-token", output.getvalue())

    def test_main_and_script_entry_return_success(self):
        import app.migrate_api as migration
        import runpy

        def close(coroutine):
            coroutine.close()

        with patch("app.migrate_api.Config.load", return_value=MagicMock()), \
                patch("asyncio.run", side_effect=close), patch("sys.argv", ["migrate_api"]):
            self.assertEqual(migration.main([]), 0)
            with self.assertRaises(SystemExit) as exit_:
                runpy.run_path(migration.__file__, run_name="__main__")
            self.assertEqual(exit_.exception.code, 0)


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_polling_until_migration_is_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config("123:fake", frozenset({123}), "http://metube", MAX_UPLOAD_BYTES, 1800,
                            api_base="http://api", download_dir=Path(tmp))
            service = VideoBot(config)
            with patch.object(service, "poll", new_callable=AsyncMock) as polling:
                with self.assertRaises(SystemExit):
                    await service.run()
                polling.assert_not_awaited()
                write_migration(Path(tmp), config.token, config.api_base, "ready")
                await service.run()
                polling.assert_awaited_once()

    async def test_unknown_migration_stage_does_not_reauthenticate_cloud(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config("123:fake", frozenset({123}), "http://metube", MAX_UPLOAD_BYTES, 1800,
                            api_base="http://api", download_dir=root)
            write_migration(root, config.token, config.api_base, "unexpected")
            with patch("app.migrate_api.Bot") as bot:
                with self.assertRaisesRegex(RuntimeError, "Unknown"):
                    await migrate(config)
                bot.assert_not_called()
