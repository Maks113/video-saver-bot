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
if python3 scripts/prepare-local.py; then
  bash scripts/switch-local-api.sh
else
  echo "MeTube started. Configure credentials in .env, then run: sudo bash scripts/switch-local-api.sh"
fi
