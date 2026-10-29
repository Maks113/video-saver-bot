"""Bounded yt-dlp subprocess; no Telegram dependency."""
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys
import tempfile
from urllib.parse import urlsplit

MAX_UPLOAD_BYTES = 2_000_000_000
MAX_WORK_BYTES = 3 * MAX_UPLOAD_BYTES + 100_000_000
DISK_RESERVE_BYTES = 1_000_000_000
CLEANUP_TIMEOUT = 2
ALLOWED_DOMAINS = {
    "youtube.com": "youtube", "youtu.be": "youtube",
    "instagram.com": "instagram",
    "vk.com": "vk", "vkvideo.ru": "vk", "vk.ru": "vk",
    "tiktok.com": "tiktok",
}


class DownloadError(Exception):
    pass


class AccessRestricted(DownloadError):
    """The platform explicitly requires authentication or restricts the audience."""


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


def make_command(url: str, directory: Path, site: str, limit: int, *, use_cookies=True) -> list[str]:
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config", "--no-playlist", "--playlist-items", "1",
        "--no-progress", "--quiet", "--no-warnings",
        "--socket-timeout", "20", "--retries", "2", "--fragment-retries", "2",
        "--use-extractors", "youtube.*,instagram.*,vk.*,tiktok.*",
        "--match-filters", "!is_live",
        "--max-filesize", str(limit),
        "--format", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        # yt-dlp's res sorting uses the short side, including portrait videos.
        # This is a preference, not a filter excluding higher-only formats.
        "--format-sort", "res:720,vcodec:h264",
        "--merge-output-format", "mp4",
        "--output", str(directory / "video.%(ext)s"),
        "--print", "after_move:%(filepath)j",
    ]
    source = Path("/cookies") / (site + ".txt")
    if use_cookies and source.is_file():
        # yt-dlp may write its cookie jar; mount the original read-only.
        target = directory / "cookies.txt"
        shutil.copyfile(source, target)
        target.chmod(0o600)
        cmd.extend(["--cookies", str(target)])
    cmd.extend(["--", url])
    return cmd


def _consume_task_result(task):
    if not task.cancelled():
        task.exception()


async def stop_process(proc, reader=None):
    # The leader may have exited while descendants still own the pipe handles.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    waiters = {asyncio.create_task(proc.wait())}
    if reader is not None:
        waiters.add(reader)
    for task in waiters:
        task.add_done_callback(_consume_task_result)
    try:
        _, pending = await asyncio.wait(waiters, timeout=CLEANUP_TIMEOUT)
    finally:
        pending = {task for task in waiters if not task.done()}
        if pending:
            # asyncio.Process has no public close(). Close its CPython transport
            # only when inherited handles did not drain within the deadline.
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                transport.close()
            for task in pending:
                task.cancel()
    if pending:
        # Cancellation and pipe closure also have a finite grace period.
        await asyncio.wait(pending, timeout=CLEANUP_TIMEOUT)


def work_size(directory: Path) -> int:
    size = 0
    for path in directory.iterdir():
        try:
            info = path.stat()
        except FileNotFoundError:
            continue  # A downloader/merger renamed the file during this scan.
        if stat.S_ISREG(info.st_mode):
            size += info.st_size
    return size


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


async def _download_attempt(url: str, directory: Path, site: str, limit: int, *, use_cookies=True) -> Path:
    proc = await asyncio.create_subprocess_exec(
        *make_command(url, directory, site, limit, use_cookies=use_cookies),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env=child_environment(),
    )
    reader = asyncio.create_task(proc.communicate())
    try:
        while not reader.done():
            await asyncio.sleep(0.05)
            if work_size(directory) > min(MAX_WORK_BYTES, 3 * limit + 100_000_000):
                raise TooLarge
            if shutil.disk_usage(directory).free < DISK_RESERVE_BYTES:
                raise DownloadError("На сервере недостаточно свободного места для скачивания.")
        stdout, stderr = await reader
    finally:
        cleanup = asyncio.create_task(stop_process(proc, reader))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # A cancellation arriving during cleanup must not abandon the group.
            await asyncio.shield(cleanup)
            raise
    error = stderr.decode("utf-8", errors="replace").lower()
    if "max-filesize" in error or "larger than" in error or "maximum file size" in error:
        raise TooLarge
    if proc.returncode:
        if any(word in error for word in ("cookies", "login", "sign in", "log in", "403",
                                           "content isn't available to everyone", "certain audiences")):
            raise AccessRestricted("Площадка ограничила доступ: нужна действующая авторизация аккаунта с доступом к ролику.")
        if "429" in error:
            raise DownloadError("Площадка ограничила доступ: может потребоваться авторизация или повтор позже.")
        raise DownloadError("Не удалось скачать видео. Проверь доступность ссылки или попробуй MeTube.")
    lines = stdout.decode("utf-8", errors="replace").splitlines()
    if not lines:
        raise DownloadError("Видео не скачано: возможно, оно превышает лимит или недоступно. Попробуй MeTube.")
    try:
        path = Path(json.loads(lines[-1]))
    except (ValueError, TypeError):
        raise DownloadError("Не удалось определить скачанный файл.") from None
    return checked_file(path, directory, limit)


def child_environment():
    """No inherited bot/application tokens, session variables or Python hooks."""
    env = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8",
           "HOME": "/tmp", "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
           "XDG_CACHE_HOME": "/tmp/cache", "DENO_DIR": "/tmp/deno",
           "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


async def download(url: str, directory: Path, *, limit=MAX_UPLOAD_BYTES, timeout=600) -> Path:
    url, site = validate_url(url)
    if shutil.disk_usage(directory).free < 3 * limit + DISK_RESERVE_BYTES:
        raise DownloadError("На сервере недостаточно места для видео и временных дорожек.")
    async with asyncio.timeout(timeout):
        # Instagram uses the account only after an explicit access rejection.
        # Each attempt gets fresh files; both share the same timeout budget.
        attempts = (False, True) if site == "instagram" else (True,)
        for use_cookies in attempts:
            with tempfile.TemporaryDirectory(prefix="attempt-", dir=directory) as tmp:
                try:
                    path = await _download_attempt(url, Path(tmp), site, limit,
                                                   use_cookies=use_cookies)
                except AccessRestricted:
                    if site == "instagram" and not use_cookies and (Path("/cookies") / "instagram.txt").is_file():
                        continue
                    raise
                result = directory / ("video" + path.suffix)
                path.replace(result)
                return checked_file(result, directory, limit)
