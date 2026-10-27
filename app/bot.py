import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import tempfile

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from urllib.parse import urlsplit

from app.downloader import DownloadError, TooLarge, validate_url, MAX_UPLOAD_BYTES
from app.download_client import clear_requests, download
from app.runtime import local_session, require_local_ready, workspace

log = logging.getLogger(__name__)
STATUS_TIMEOUT = 10


@dataclass(frozen=True)
class Config:
    token: str
    users: frozenset[int]
    metube_url: str
    limit: int
    timeout: int
    api_base: str = "http://telegram-bot-api:8081"
    download_dir: Path | None = None
    upload_timeout: int = 7200
    state_dir: Path | None = None
    request_dir: Path | None = None
    result_dir: Path | None = None
    worker_output_dir: Path | None = None

    @classmethod
    def load(cls):
        token = os.environ.get("TG_BOT_TOKEN", "").strip()
        try:
            users = frozenset(int(v.strip()) for v in os.environ.get("TG_ALLOWED_USER_IDS", "").split(",") if v.strip())
            limit = int(os.environ.get("MAX_UPLOAD_BYTES", str(MAX_UPLOAD_BYTES)))
            timeout = int(os.environ.get("DOWNLOAD_TIMEOUT", "1800"))
            upload_timeout = int(os.environ.get("UPLOAD_TIMEOUT", "7200"))
        except ValueError:
            raise SystemExit("Invalid numeric configuration.") from None
        if not token or not users or any(v <= 0 for v in users):
            raise SystemExit("Set TG_BOT_TOKEN and TG_ALLOWED_USER_IDS in .env.")
        if not 0 < limit <= MAX_UPLOAD_BYTES or not 0 < timeout <= 7200 or not 0 < upload_timeout <= 14400:
            raise SystemExit("Invalid download limits.")
        base = os.environ.get("BOT_API_BASE", "http://telegram-bot-api:8081").rstrip("/")
        try:
            parsed = urlsplit(base)
            port = parsed.port
        except ValueError:
            raise SystemExit("Invalid BOT_API_BASE.") from None
        root = Path(os.environ.get("DOWNLOAD_DIR", "/uploads"))
        state = Path(os.environ.get("STATE_DIR", "/state"))
        requests = Path(os.environ.get("REQUEST_DIR", "/requests"))
        results = Path(os.environ.get("RESULT_DIR", "/results"))
        worker_output = Path(os.environ.get("WORKER_OUTPUT_DIR", "/downloads"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or port == 0 or not root.is_absolute():
            raise SystemExit("Invalid BOT_API_BASE or DOWNLOAD_DIR.")
        if not all(path.is_absolute() for path in (state, requests, results, worker_output)):
            raise SystemExit("Worker paths must be absolute.")
        return cls(token, users, os.environ.get("METUBE_URL", "http://localhost:8081"), limit, timeout,
                   base, root, upload_timeout, state, requests, results, worker_output)


class VideoBot:
    def __init__(self, config: Config):
        self.config = config
        self.queue = asyncio.Queue(maxsize=8)
        self.pending: set[int] = set()
        self.dp = Dispatcher()
        self.dp.message.register(self.identify, Command("id"))
        self.dp.message.register(self.help, Command("start", "help"))
        self.dp.message.register(self.metube, Command("metube"))
        self.dp.message.register(self.download_command, Command("download"))
        self.dp.message.register(self.receive, F.text)

    def allowed(self, message: Message) -> bool:
        return bool(
            message.from_user
            and message.from_user.id in self.config.users
            and not message.sender_chat
            and message.chat.type in {"private", "group", "supergroup"}
        )

    async def identify(self, message: Message):
        if message.chat.type == "private" and message.from_user:
            await message.answer(f"Твой Telegram ID: {message.from_user.id}")

    async def help(self, message: Message):
        if self.allowed(message):
            await message.answer(
                "В личном чате пришли ссылку на Instagram, YouTube, VK Видео или TikTok.\n"
                "В группе: /download@M113_video_saver_bot ссылка\n"
                "Также можно отправить /download ответом на сообщение со ссылкой.\n"
                f"Качество выбирается автоматически; файл до {self.config.limit / 1_000_000:g} МБ.\n"
                + ("Большие видео: /metube\n" if self.config.metube_url else "")
                + "Одна задача на пользователя; ссылки обрабатываются по очереди."
            )

    async def metube(self, message: Message):
        if self.allowed(message):
            await message.answer("MeTube:\n" + self.config.metube_url if self.config.metube_url
                                 else "MeTube на этом сервере отключён.")

    async def download_command(self, message: Message, command: CommandObject):
        if not self.allowed(message):
            return
        text = command.args
        if not text and message.reply_to_message:
            text = message.reply_to_message.text or message.reply_to_message.caption
        await self.enqueue(message, text or "")

    async def receive(self, message: Message):
        if not self.allowed(message) or message.chat.type != "private":
            return
        await self.enqueue(message, message.text or "")

    async def enqueue(self, message: Message, text: str):
        try:
            url, _ = validate_url(text)
        except DownloadError as exc:
            await message.answer(str(exc))
            return
        user_id = message.from_user.id
        if user_id in self.pending:
            await message.answer("Предыдущее видео ещё обрабатывается.")
            return
        if self.queue.full():
            await message.answer("Очередь заполнена. Попробуй позже.")
            return
        status = await message.answer("Добавлено в очередь.")
        # Recheck after network await so simultaneous updates cannot bypass the limit.
        if user_id in self.pending or self.queue.full():
            await self.update_status(status, "Предыдущее видео ещё обрабатывается или очередь заполнена.")
            return
        self.pending.add(user_id)
        self.queue.put_nowait((message, status, url, user_id))

    async def update_status(self, status: Message, text: str):
        try:
            async with asyncio.timeout(STATUS_TIMEOUT):
                await status.edit_text(text)
        except (TelegramAPIError, TimeoutError):
            # Status is optional; deleted messages/network errors cannot abort a job.
            log.debug("Status update unavailable")

    def fallback_hint(self):
        return "\nMeTube:\n" + self.config.metube_url if self.config.metube_url else ""

    async def worker(self):
        while True:
            message, status, url, user_id = await self.queue.get()
            try:
                await self.update_status(status, "Скачиваю видео…")
                with tempfile.TemporaryDirectory(prefix="video-saver-", dir=self.config.download_dir) as tmp:
                    try:
                        path = await download(url, Path(tmp), limit=self.config.limit, timeout=self.config.timeout,
                                              request_dir=self.config.request_dir, result_dir=self.config.result_dir,
                                              output_root=self.config.worker_output_dir)
                    except TooLarge:
                        await self.update_status(status, f"Видео больше лимита {self.config.limit / 1_000_000:g} МБ." + self.fallback_hint())
                        continue
                    except TimeoutError:
                        await self.update_status(status, f"Скачивание превысило лимит {self.config.timeout:g} с." + self.fallback_hint())
                        continue
                    except DownloadError as exc:
                        await self.update_status(status, str(exc))
                        continue
                    # Check actual final bytes immediately before uploading.
                    if path.stat().st_size > self.config.limit:
                        await self.update_status(status, f"Видео больше лимита {self.config.limit / 1_000_000:g} МБ." + self.fallback_hint())
                        continue
                    await self.update_status(status, "Отправляю видео…")
                    # This copy belongs to the bot. The downloader cannot modify
                    # its path or content while Local Bot API reads it.
                    file = path.as_uri()
                    if path.suffix.lower() == ".mp4":
                        await message.answer_video(file, supports_streaming=True, request_timeout=self.config.upload_timeout)
                    else:
                        await message.answer_document(file, request_timeout=self.config.upload_timeout)
                    await self.update_status(status, "Готово.")
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not log URLs, cookies, tokens or raw upstream errors.
                log.warning("Video task failed")
                await self.update_status(status, "Не удалось обработать или отправить видео. Попробуй позже.")
            finally:
                self.pending.discard(user_id)
                self.queue.task_done()

    async def run(self):
        with workspace(self.config.state_dir or self.config.download_dir,
                       cleanup_roots=(self.config.download_dir,)) as root:
            require_local_ready(root, self.config.token, self.config.api_base)
            if self.config.request_dir is not None:
                clear_requests(self.config.request_dir)
            await self.poll()

    async def poll(self):
        # Polling must recover promptly from a broken API connection. Only file
        # sends below opt into the longer per-request upload timeout.
        bot = Bot(self.config.token, session=local_session(self.config.api_base, 60))
        worker = asyncio.create_task(self.worker())
        try:
            async with bot:
                await self.dp.start_polling(bot, allowed_updates=["message"], handle_signals=True)
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(VideoBot(Config.load()).run())


if __name__ == "__main__":
    main()
