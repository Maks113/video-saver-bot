"""Validate local settings and create bind mounts without printing secrets."""
import os
from pathlib import Path
import re
import shutil

root = Path(__file__).resolve().parent.parent
values = {}
for line in (root / ".env").read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
for key in ["TG_BOT_TOKEN", "TG_ALLOWED_USER_IDS", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"]:
    if not values.get(key):
        raise SystemExit(f"Set {key} in .env before migration.")
if not values["TELEGRAM_API_ID"].isdecimal() or int(values["TELEGRAM_API_ID"]) <= 0:
    raise SystemExit("TELEGRAM_API_ID must be a positive number.")
if not re.fullmatch(r"[a-fA-F0-9]{32}", values["TELEGRAM_API_HASH"]):
    raise SystemExit("TELEGRAM_API_HASH must contain 32 hexadecimal characters.")
uid, gid = int(values.get("PUID", "1000")), int(values.get("PGID", "1000"))
if os.geteuid() not in {0, uid}:
    raise SystemExit("PUID must match the current user, or run preparation as root.")
limit = int(values.get("MAX_UPLOAD_BYTES", "2000000000"))
if not 0 < limit <= 2_000_000_000:
    raise SystemExit("MAX_UPLOAD_BYTES must be between 1 and 2000000000.")
if shutil.disk_usage(root).free < 3 * limit + 1_000_000_000:
    raise SystemExit("Insufficient free disk for media, merge tracks and reserve.")
for name in ["data/bot-api", "data/bot-api-tmp", "data/bot-downloads", "cookies"]:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(directory, uid, gid)
    directory.chmod(0o700)
print("Local API settings and directories ready; no secrets printed.")
