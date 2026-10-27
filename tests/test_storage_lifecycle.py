import asyncio
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.prepare_storage import prepare
from app.runtime import MARKER, migration_state, write_migration


class StorageTests(unittest.TestCase):
    def test_ownership_private_directories_cookies_and_legacy_state_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "legacy").mkdir()
            (root / "cookies").mkdir()
            cookie = root / "cookies/youtube.txt"
            cookie.write_text("private cookie")
            (root / "cookies/instagram.txt").symlink_to(cookie)
            write_migration(root / "legacy", "123:fake", "http://api", "ready")
            with patch("app.prepare_storage.os.chown") as owners, \
                    patch.dict("os.environ", {"PUID": "1000", "PGID": "1000", "DOWNLOADER_UID": "65532"}):
                prepare(root)
                self.assertEqual((root / "state").stat().st_mode & 0o777, 0o700)
                self.assertEqual((root / "uploads").stat().st_mode & 0o777, 0o700)
                self.assertEqual((root / "downloads").stat().st_mode & 0o777, 0o750)
                self.assertEqual(cookie.stat().st_mode & 0o777, 0o640)
                owners.assert_any_call(root / "downloads", 65532, 1000)
                owners.assert_any_call(root / "state", 1000, 1000)
                self.assertEqual(migration_state(root / "state")["stage"], "ready")
                write_migration(root / "legacy", "999:fake", "http://other", "logout_requested")
                prepare(root)
                self.assertEqual(migration_state(root / "state")["bot_id"], "123")

    def test_invalid_user_ids_fail_before_touching_files(self):
        for env in [{"PUID": "0"}, {"DOWNLOADER_UID": "0"},
                    {"PUID": "1000", "DOWNLOADER_UID": "1000"}, {"PGID": "-1"}]:
            with self.subTest(env=env), patch.dict("os.environ",
                    {"PUID": "1000", "PGID": "1000", "DOWNLOADER_UID": "65532", **env}), \
                    patch("app.prepare_storage.os.chown") as owners:
                with self.assertRaises(SystemExit):
                    prepare(Path("/must-not-touch"))
                owners.assert_not_called()

    def test_script_rejects_root_bot_configuration(self):
        import app.prepare_storage as module
        with patch.dict("os.environ", {"PUID": "0"}):
            with self.assertRaises(SystemExit):
                runpy.run_path(module.__file__, run_name="__main__")


class WorkerLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_signal_handler_removed_when_worker_stops(self):
        import app.download_worker as worker
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler") as add, \
                patch.object(loop, "remove_signal_handler") as remove, \
                patch("app.download_worker.serve", new_callable=AsyncMock) as serve:
            await worker.run()
            add.assert_called_once()
            remove.assert_called_once()
            serve.assert_awaited_once()

    async def test_script_stops_cleanly_on_interrupt(self):
        import app.download_worker as worker
        def interrupt(coroutine):
            coroutine.close()
            raise KeyboardInterrupt
        with patch("asyncio.run", side_effect=interrupt):
            runpy.run_path(worker.__file__, run_name="__main__")
