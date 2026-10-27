"""Small untrusted messages; never follow downloader-controlled symlinks."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import stat

from app.downloader import DownloadError

JOB_NAME = re.compile(r"[0-9a-f]{32}\.json\Z")
JSON_LIMIT = 65_536


def read_json(root: Path, name: str):
    with ExitStack() as stack:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        stack.callback(os.close, file)
        info = os.fstat(file)
        if not stat.S_ISREG(info.st_mode) or info.st_size > JSON_LIMIT:
            raise DownloadError("Некорректный ответ загрузчика.")
        data = os.read(file, JSON_LIMIT + 1)
    if len(data) > JSON_LIMIT:
        raise DownloadError("Слишком большой ответ загрузчика.")
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise DownloadError("Некорректный ответ загрузчика.") from None
    if not isinstance(result, dict):
        raise DownloadError("Некорректный ответ загрузчика.")
    return result


def write_json(root: Path, name: str, value):
    # Each writer owns its directory; the opposite container mounts it read-only.
    pending = root / (name + ".tmp")
    with pending.open("w") as file:
        os.fchmod(file.fileno(), 0o640)
        json.dump(value, file)
    pending.replace(root / name)
