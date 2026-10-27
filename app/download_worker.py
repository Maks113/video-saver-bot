"""Downloader-only container: no Telegram token, Bot API network or bot state mount."""
import asyncio
from contextlib import suppress
import os
import math
from pathlib import Path
import signal
import time

from app.downloader import DownloadError, MAX_UPLOAD_BYTES, TooLarge, download, validate_url
from app.job_protocol import JOB_NAME, read_json, write_json
from app.runtime import workspace


def heartbeat():
    Path("/tmp/downloader-heartbeat").touch()


async def cleanup_job(task, root, result):
    if task is not None:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
    if root.exists():
        import shutil
        shutil.rmtree(root)
    result.unlink(missing_ok=True)


async def serve_job(requests: Path, results: Path, outputs: Path, name: str):
    root = outputs / ("video-saver-" + name[:-5])
    request = requests / name
    task = None
    deadline = time.time()
    try:
        try:
            data = read_json(requests, name)
            url, _ = validate_url(data["url"])
            limit = data["limit"]
            expiration = data["expires_at"]
            if type(expiration) not in {float, int} or not math.isfinite(expiration):
                raise ValueError("Invalid deadline")
            deadline = expiration
            if type(limit) is not int or not 0 < limit <= MAX_UPLOAD_BYTES:
                raise ValueError("Invalid limit")
            remaining = deadline - time.time()
            if not 0 < remaining <= 7200:
                raise TimeoutError
            root.mkdir(mode=0o700)
            task = asyncio.create_task(download(url, root, limit=limit, timeout=remaining))
            while not task.done():
                heartbeat()
                if not request.exists():
                    return
                await asyncio.wait({task}, timeout=0.1)
            path = await task
            path.chmod(0o640)
            root.chmod(0o750)
            reply = {"ok": True, "filename": path.name}
        except TooLarge:
            reply = {"ok": False, "error": "too_large"}
        except TimeoutError:
            reply = {"ok": False, "error": "timeout"}
        except (DownloadError, KeyError, TypeError, ValueError):
            reply = {"ok": False, "error": "download_failed"}
        except OSError:
            reply = {"ok": False, "error": "storage_failed"}
        except Exception:
            reply = {"ok": False, "error": "download_failed"}
        write_json(results, name, reply)
        # The client copies into its private upload volume before releasing us.
        release_deadline = max(time.time(), deadline) + 30
        while request.exists() and time.time() < release_deadline:
            heartbeat()
            await asyncio.sleep(0.1)
    finally:
        cleanup = asyncio.create_task(cleanup_job(task, root, results / name))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await asyncio.shield(cleanup)
            raise


async def serve(requests: Path, results: Path, outputs: Path):
    with workspace(outputs):
        for path in results.glob("*.json*"):
            path.unlink()
        seen = set()
        while True:
            heartbeat()
            names = {p.name for p in requests.iterdir() if JOB_NAME.fullmatch(p.name)}
            seen.intersection_update(names)
            for name in sorted(names - seen):
                seen.add(name)
                await serve_job(requests, results, outputs, name)
            await asyncio.sleep(0.1)


async def run():
    current = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, current.cancel)
    try:
        await serve(Path(os.environ.get("REQUEST_DIR", "/requests")),
                    Path(os.environ.get("RESULT_DIR", "/results")),
                    Path(os.environ.get("OUTPUT_DIR", "/downloads")))
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
