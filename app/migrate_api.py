"""One-time cloud logOut followed by local authentication. Never prints tokens."""
import asyncio
import sys

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

from app.bot import Config
from app.runtime import local_session, migration_state, workspace, write_migration


async def migrate(config):
    with workspace(config.download_dir) as root:
        state = migration_state(root)
        identity = {"bot_id": config.token.split(":", 1)[0], "api_base": config.api_base}
        if state and any(state.get(key) != value for key, value in identity.items()):
            raise RuntimeError("Migration state belongs to a different bot/API server")
        if state and state.get("stage") not in {"cloud_logged_out", "ready"}:
            raise RuntimeError("Unknown migration stage")
        if state.get("stage") not in {"cloud_logged_out", "ready"}:
            async with Bot(config.token, session=AiohttpSession(timeout=60)) as cloud:
                if not await cloud.log_out():
                    raise RuntimeError("Cloud logOut was not confirmed")
            # Persist confirmation before local auth, so a retry never reauthenticates
            # against cloud after it has successfully logged out.
            write_migration(root, config.token, config.api_base, "cloud_logged_out")
        async with Bot(config.token, session=local_session(config.api_base, 60)) as local:
            me = await local.get_me()
            if str(me.id) != identity["bot_id"]:
                raise RuntimeError("Unexpected bot identity from Local Bot API")
        write_migration(root, config.token, config.api_base, "ready")
        print("Local Bot API migration confirmed; polling may start.")


def main():
    try:
        asyncio.run(migrate(Config.load()))
    except Exception as exc:
        print(f"Migration failed ({type(exc).__name__}); polling remains stopped.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
