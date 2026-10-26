#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $EUID -ne 0 ]]; then
  echo "Run: sudo bash scripts/install-server.sh" >&2
  exit 1
fi
apt-get update
apt-get install -y docker.io docker-compose-v2
systemctl enable --now docker
docker compose config --quiet
docker compose up -d --build metube
if grep -Eq '^TG_BOT_TOKEN=[0-9]+:[A-Za-z0-9_-]+' .env; then
  docker compose --profile bot up -d --build bot
else
  echo "MeTube started. Set TG_BOT_TOKEN in .env, then run: sudo docker compose --profile bot up -d --build"
fi
docker compose --profile bot ps
