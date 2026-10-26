"""Persistent workspace ownership and explicit cloud-to-local migration state."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import shutil

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

MARKER = ".local-api.json"


def local_session(base: str, timeout: int):
    return AiohttpSession(api=TelegramAPIServer.from_base(base, is_local=True), timeout=timeout)


@contextmanager
def workspace(root: Path):
    if root is None:
        raise SystemExit("DOWNLOAD_DIR is required for Local Bot API.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another bot or migration already owns DOWNLOAD_DIR.") from None
        # The exclusive lock ensures there are no live jobs in this directory.
        for path in root.glob("video-saver-*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        yield root


def migration_state(root: Path):
    try:
        value = json.loads((root / MARKER).read_text())
    except FileNotFoundError:
        return {}
    except ValueError:
        raise SystemExit("Invalid Local Bot API migration state.") from None
    if not isinstance(value, dict):
        raise SystemExit("Invalid Local Bot API migration state.")
    return value


def write_migration(root: Path, token: str, base: str, stage: str):
    value = {"bot_id": token.split(":", 1)[0], "api_base": base, "stage": stage}
    pending = root / (MARKER + ".tmp")
    pending.write_text(json.dumps(value))
    pending.chmod(0o600)
    pending.replace(root / MARKER)


def require_local_ready(root: Path, token: str, base: str):
    expected = {"bot_id": token.split(":", 1)[0], "api_base": base, "stage": "ready"}
    if migration_state(root) != expected:
        raise SystemExit("Run scripts/switch-local-api.sh before starting local polling.")
