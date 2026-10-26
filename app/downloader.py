"""Bounded yt-dlp subprocess; no Telegram dependency."""
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
from urllib.parse import urlsplit

MAX_UPLOAD_BYTES = 50_000_000
MAX_WORK_BYTES = 110_000_000
ALLOWED_DOMAINS = {
    "youtube.com": "youtube", "youtu.be": "youtube",
    "instagram.com": "instagram",
    "vk.com": "vk", "vkvideo.ru": "vk", "vk.ru": "vk",
    "tiktok.com": "tiktok",
}


class DownloadError(Exception):
    pass


class TooLarge(DownloadError):
    pass


def validate_url(text: str) -> tuple[str, str]:
    matches = re.findall(r"https?://[^\s<>]+", text)
    if len(matches) != 1:
        raise DownloadError("Пришли одну ссылку на видео.")
    url = matches[0].rstrip(".,!);]}")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise ValueError
    except ValueError:
        raise DownloadError("Некорректная ссылка.") from None
    for domain, site in ALLOWED_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return url, site
    raise DownloadError("Поддерживаются Instagram, YouTube, VK Видео и TikTok.")


def make_command(url: str, directory: Path, site: str, limit: int) -> list[str]:
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config", "--no-playlist", "--playlist-items", "1",
        "--no-progress", "--quiet", "--no-warnings",
        "--socket-timeout", "20", "--retries", "2", "--fragment-retries", "2",
        "--use-extractors", "youtube.*,instagram.*,vk.*,tiktok.*",
        "--match-filters", "!is_live",
        "--max-filesize", str(limit),
        "--format", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]",
        "--format-sort", "vcodec:h264",
        "--merge-output-format", "mp4",
        "--output", str(directory / "video.%(ext)s"),
        "--print", "after_move:%(filepath)j",
    ]
    source = Path("/cookies") / (site + ".txt")
    if source.is_file():
        # yt-dlp may write its cookie jar; mount the original read-only.
        target = directory / "cookies.txt"
        shutil.copyfile(source, target)
        target.chmod(0o600)
        cmd.extend(["--cookies", str(target)])
    cmd.extend(["--", url])
    return cmd


async def stop_process(proc):
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await proc.wait()


def work_size(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.iterdir() if p.is_file())


def checked_file(path: Path, directory: Path, limit: int) -> Path:
    resolved = path.resolve()
    if resolved.parent != directory.resolve() or not resolved.is_file():
        raise DownloadError("Сервис не вернул видеофайл.")
    size = resolved.stat().st_size
    if size > limit:
        raise TooLarge
    if not size or resolved.suffix.lower() not in {".mp4", ".mkv", ".webm", ".mov"}:
        raise DownloadError("Не удалось получить видеофайл.")
    return resolved


async def download(url: str, directory: Path, *, limit=MAX_UPLOAD_BYTES, timeout=600) -> Path:
    url, site = validate_url(url)
    proc = await asyncio.create_subprocess_exec(
        *make_command(url, directory, site, limit),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    reader = asyncio.create_task(proc.communicate())
    try:
        async with asyncio.timeout(timeout):
            while not reader.done():
                await asyncio.sleep(0.25)
                if work_size(directory) > MAX_WORK_BYTES:
                    raise TooLarge
            stdout, stderr = await reader
    except BaseException:
        await stop_process(proc)
        await reader
        raise
    error = stderr.decode("utf-8", errors="replace").lower()
    if "max-filesize" in error or "larger than" in error or "maximum file size" in error:
        raise TooLarge
    if proc.returncode:
        if any(word in error for word in ("cookies", "login", "sign in", "log in", "403", "429")):
            raise DownloadError("Площадка ограничила доступ: может потребоваться авторизация или повтор позже.")
        raise DownloadError("Не удалось скачать видео. Проверь доступность ссылки или попробуй MeTube.")
    lines = stdout.decode("utf-8", errors="replace").splitlines()
    if not lines:
        # yt-dlp can skip oversized formats without a nonzero exit status.
        raise DownloadError("Видео не скачано: возможно, оно больше 50 МБ или недоступно. Попробуй MeTube.")
    try:
        path = Path(json.loads(lines[-1]))
    except (ValueError, TypeError):
        raise DownloadError("Не удалось определить скачанный файл.") from None
    return checked_file(path, directory, limit)
