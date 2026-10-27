"""Bot-side file transport to a downloader that has no Telegram credentials."""
import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import stat
import time
import uuid

from app.downloader import DISK_RESERVE_BYTES, DownloadError, TooLarge
from app.job_protocol import JOB_NAME, read_json, write_json

EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov"}


def clear_requests(root: Path):
    for path in root.iterdir():
        if JOB_NAME.fullmatch(path.name) or JOB_NAME.fullmatch(path.name.removesuffix(".tmp")):
            path.unlink(missing_ok=True)


async def file_io(function, *args):
    # Never close/reuse an fd while an already scheduled disk read still owns it.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def copy_result(root: Path, job: str, filename: str, destination: Path, limit: int):
    if not isinstance(filename, str) or filename not in {"video" + ext for ext in EXTENSIONS}:
        raise DownloadError("Загрузчик вернул недопустимый файл.")
    target = destination / filename
    created = False
    try:
        with ExitStack() as stack:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, root_fd)
            job_fd = os.open("video-saver-" + job, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=root_fd)
            stack.callback(os.close, job_fd)
            source = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=job_fd)
            stack.callback(os.close, source)
            info = os.fstat(source)
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise DownloadError("Загрузчик не вернул обычный видеофайл.")
            if info.st_size > limit:
                raise TooLarge
            if shutil.disk_usage(destination).free < info.st_size + DISK_RESERVE_BYTES:
                raise DownloadError("Недостаточно места для безопасной копии видео.")
            output = stack.enter_context(target.open("xb"))
            created = True
            os.fchmod(output.fileno(), 0o600)
            total = 0
            while chunk := await file_io(os.read, source, min(1_048_576, limit - total + 1)):
                total += len(chunk)
                if total > limit:
                    raise TooLarge
                if shutil.disk_usage(destination).free < DISK_RESERVE_BYTES:
                    raise DownloadError("Недостаточно свободного места для видео.")
                await file_io(output.write, chunk)
            if total == 0:
                raise DownloadError("Загрузчик вернул пустой файл.")
        return target
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


async def download(url: str, directory: Path, *, limit: int, timeout: float,
                   request_dir: Path, result_dir: Path, output_root: Path):
    job = uuid.uuid4().hex
    name = job + ".json"
    request = request_dir / name
    try:
        async with asyncio.timeout(timeout):
            write_json(request_dir, name, {"url": url, "limit": limit,
                                          "expires_at": time.time() + timeout})
            while True:
                try:
                    result = read_json(result_dir, name)
                except FileNotFoundError:
                    await asyncio.sleep(0.05)
                    continue
                if result.get("ok") is not True:
                    if result.get("error") == "too_large":
                        raise TooLarge
                    if result.get("error") == "timeout":
                        raise TimeoutError
                    raise DownloadError("Не удалось скачать видео. Проверь доступность ссылки.")
                return await copy_result(output_root, job, result.get("filename"), directory, limit)
    finally:
        # Absence of the request cancels the job/releases downloader-owned files.
        request.unlink(missing_ok=True)
