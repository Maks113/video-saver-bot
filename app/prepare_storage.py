"""One-shot root container; only explicit data mounts, no network or bot token."""
import os
from pathlib import Path

from app.runtime import MARKER, migration_state, write_migration


def prepare(root: Path = Path("/")):
    bot_uid = int(os.environ.get("PUID", "1000"))
    group = int(os.environ.get("PGID", "1000"))
    worker_uid = int(os.environ.get("DOWNLOADER_UID", "65532"))
    if bot_uid <= 0 or worker_uid <= 0 or worker_uid == bot_uid or group < 0:
        raise SystemExit("Bot and downloader must have distinct non-root UIDs.")
    for name, owner, mode in [
        ("/state", bot_uid, 0o700), ("/uploads", bot_uid, 0o700),
        ("/requests", bot_uid, 0o750), ("/results", worker_uid, 0o750),
        ("/downloads", worker_uid, 0o750), ("/cookies", bot_uid, 0o750),
        ("/api-state", bot_uid, 0o700), ("/api-tmp", bot_uid, 0o700),
    ]:
        path = root / name.lstrip("/")
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, owner, group)
        path.chmod(mode)
    for site in ("youtube", "instagram", "vk", "tiktok"):
        path = root / "cookies" / (site + ".txt")
        if path.is_file() and not path.is_symlink():
            os.chown(path, bot_uid, group)
            path.chmod(0o640)
    # Upgrade from the first Local API layout; never expose this state to worker.
    state = root / "state"
    if not (state / MARKER).exists() and (root / "legacy" / MARKER).is_file():
        previous = migration_state(root / "legacy")
        write_migration(state, previous["bot_id"] + ":unused", previous["api_base"], previous["stage"])
        os.chown(state / MARKER, bot_uid, group)
    print("Storage ownership and private bot state are ready.")


if __name__ == "__main__":
    prepare()
