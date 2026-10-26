import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import tempfile

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

from app.downloader import DownloadError, TooLarge, download, validate_url, MAX_UPLOAD_BYTES

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    token: str
    users: frozenset[int]
    metube_url: str
    limit: int
    timeout: int

    @classmethod
    def load(cls):
        token = os.environ.get("TG_BOT_TOKEN", "").strip()
        try:
            users = frozenset(int(v.strip()) for v in os.environ.get("TG_ALLOWED_USER_IDS", "").split(",") if v.strip())
            limit = int(os.environ.get("MAX_UPLOAD_BYTES", str(MAX_UPLOAD_BYTES)))
            timeout = int(os.environ.get("DOWNLOAD_TIMEOUT", "600"))
        except ValueError:
            raise SystemExit("Invalid numeric configuration.") from None
        if not token or not users or any(v <= 0 for v in users):
            raise SystemExit("Set TG_BOT_TOKEN and TG_ALLOWED_USER_IDS in .env.")
        if not 0 < limit <= MAX_UPLOAD_BYTES or not 0 < timeout <= 1800:
            raise SystemExit("Invalid download limits.")
        return cls(token, users, os.environ.get("METUBE_URL", "http://localhost:8081"), limit, timeout)


class VideoBot:
    def __init__(self, config: Config):
        self.config = config
        self.queue = asyncio.Queue(maxsize=8)
        self.pending: set[int] = set()
        self.dp = Dispatcher()
        self.dp.message.register(self.identify, Command("id"))
        self.dp.message.register(self.help, Command("start", "help"))
        self.dp.message.register(self.metube, Command("metube"))
        self.dp.message.register(self.receive, F.text)

    def allowed(self, message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in self.config.users and message.chat.type == "private")

    async def identify(self, message: Message):
        if message.chat.type == "private" and message.from_user:
            await message.answer(f"Твой Telegram ID: {message.from_user.id}")

    async def help(self, message: Message):
        if self.allowed(message):
            await message.answer(
                "Пришли одну ссылку на Instagram, YouTube, VK Видео или TikTok.\n"
                "Скачаю видео в качестве до 720p и отправлю файл до 50 МБ.\n"
                "Большие видео: /metube\n"
                "Одна задача на пользователя; ссылки обрабатываются по очереди."
            )

    async def metube(self, message: Message):
        if self.allowed(message):
            await message.answer("MeTube для больших видео (доступен из домашней сети):\n" + self.config.metube_url)

    async def receive(self, message: Message):
        if not self.allowed(message):
            return
        try:
            url, _ = validate_url(message.text or "")
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
            await status.edit_text("Предыдущее видео ещё обрабатывается или очередь заполнена.")
            return
        self.pending.add(user_id)
        self.queue.put_nowait((message, status, url, user_id))

    async def worker(self):
        while True:
            message, status, url, user_id = await self.queue.get()
            try:
                await status.edit_text("Скачиваю видео…")
                with tempfile.TemporaryDirectory(prefix="video-saver-") as tmp:
                    try:
                        path = await download(url, Path(tmp), limit=self.config.limit, timeout=self.config.timeout)
                    except TooLarge:
                        await status.edit_text("Видео больше 50 МБ. Скачай его через MeTube:\n" + self.config.metube_url)
                        continue
                    except TimeoutError:
                        await status.edit_text("Скачивание заняло больше 10 минут. Попробуй MeTube:\n" + self.config.metube_url)
                        continue
                    except DownloadError as exc:
                        await status.edit_text(str(exc))
                        continue
                    # Check actual final bytes immediately before uploading.
                    if path.stat().st_size > self.config.limit:
                        await status.edit_text("Видео больше 50 МБ. MeTube:\n" + self.config.metube_url)
                        continue
                    await status.edit_text("Отправляю видео…")
                    file = FSInputFile(path, filename="video" + path.suffix)
                    if path.suffix.lower() == ".mp4":
                        await message.answer_video(file, supports_streaming=True, request_timeout=180)
                    else:
                        await message.answer_document(file, request_timeout=180)
                    await status.edit_text("Готово.")
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not log URLs, cookies, tokens or raw upstream errors.
                log.warning("Video task failed")
                with suppress(TelegramAPIError):
                    await status.edit_text("Не удалось обработать или отправить видео. Попробуй позже.")
            finally:
                self.pending.discard(user_id)
                self.queue.task_done()

    async def run(self):
        bot = Bot(self.config.token, session=AiohttpSession(timeout=180))
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
