#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/prepare-local.py
docker compose --profile bot config --quiet
docker compose build bot
docker compose --profile bot run --rm --no-deps -T bot python -c 'from app.bot import Config; Config.load(); print("Bot configuration verified.")'
# Statistics readiness does not authenticate this bot locally.
docker compose --profile bot up -d --wait telegram-bot-api
docker compose --profile bot stop bot
# On failure, polling stays stopped; cloud rollback is unsafe after logOut.
docker compose --profile bot run --rm --no-deps -T bot python -m app.migrate_api
docker compose --profile bot up -d --no-deps bot
docker compose --profile bot ps
