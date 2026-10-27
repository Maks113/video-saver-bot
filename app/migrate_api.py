"""One-time cloud logOut followed by local authentication. Never prints tokens."""
import asyncio
import argparse
import sys

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramServerError

from app.bot import Config
from app.runtime import local_session, migration_state, workspace, write_migration


async def migrate(config, *, retry_cloud_logout=False):
    with workspace(config.state_dir or config.download_dir) as root:
        state = migration_state(root)
        identity = {"bot_id": config.token.split(":", 1)[0], "api_base": config.api_base}
        if state and any(state.get(key) != value for key, value in identity.items()):
            raise RuntimeError("Migration state belongs to a different bot/API server")
        stage = state.get("stage")
        if state and stage not in {"logout_requested", "logout_rejected", "cloud_logged_out", "ready", "ready_unconfirmed"}:
            raise RuntimeError("Unknown migration stage")
        logout_confirmed = stage in {"cloud_logged_out", "ready"}
        if stage == "logout_rejected" and not retry_cloud_logout:
            raise RuntimeError("Cloud logOut was rejected; inspect the cause before an explicit retry")
        if not state or (retry_cloud_logout and stage in {"logout_requested", "logout_rejected", "ready_unconfirmed"}):
            # Persist and fsync intent BEFORE the RPC. A timeout, cancellation,
            # lost response or crash must never trigger another cloud call by default.
            write_migration(root, config.token, config.api_base, "logout_requested")
            async with Bot(config.token, session=AiohttpSession(timeout=60)) as cloud:
                try:
                    confirmed = await cloud.log_out()
                except (TelegramNetworkError, TelegramServerError):
                    raise  # The server may already have performed the operation.
                except TelegramAPIError:
                    write_migration(root, config.token, config.api_base, "logout_rejected")
                    raise
                if not confirmed:
                    write_migration(root, config.token, config.api_base, "logout_rejected")
                    raise RuntimeError("Cloud logOut was not confirmed")
                write_migration(root, config.token, config.api_base, "cloud_logged_out")
                logout_confirmed = True
        # logout_requested is an uncertain outcome, not permission to repeat the
        # cloud RPC. A subsequent run tries local auth only; failure keeps polling off.
        async with Bot(config.token, session=local_session(config.api_base, 60)) as local:
            me = await local.get_me()
            if str(me.id) != identity["bot_id"]:
                raise RuntimeError("Unexpected bot identity from Local Bot API")
        write_migration(root, config.token, config.api_base, "ready" if logout_confirmed else "ready_unconfirmed")
        if logout_confirmed:
            print("Local Bot API migration confirmed; polling may start.")
        else:
            print("Local authentication verified; previous cloud logOut outcome remains uncertain.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry-cloud-logout", action="store_true",
                        help="Explicit operator retry after investigating an uncertain/rejected logOut")
    args = parser.parse_args(argv)
    try:
        asyncio.run(migrate(Config.load(), retry_cloud_logout=args.retry_cloud_logout))
    except Exception as exc:
        print(f"Migration failed ({type(exc).__name__}); polling remains stopped.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
