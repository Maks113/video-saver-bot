# Перенос на VPS: 1 vCPU, 1 ГБ RAM, 5 ГБ свободного SSD

Вариант без MeTube, лимит видео **250 000 000 байт (250 МБ)**.
Бот, изолированный downloader и Local Bot API используют один образ приложения
(около 1,07 ГБ на текущей версии) и образ API (около 66 МБ). Образ MeTube не загружается.
Это минимальная конфигурация: только одно скачивание, без перекодирования.
Размеры образов зависят от архитектуры. После установки/сборки должно оставаться
не менее **1,75 ГБ свободного места**; проверка встроена в скрипт и в загрузчик.

## 1. Подготовить новый сервер

Команды установки ниже рассчитаны на Ubuntu 24.04. На другой ОС установите
Docker Engine и Compose по её инструкции. Домен и белый IP для бота не требуются.

```bash
sudo apt-get update
sudo apt-get install -y git docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo mkdir -p /opt/video-saver-bot
sudo git clone https://github.com/Maks113/video-saver-bot.git /opt/video-saver-bot
```

Далее команды выполняются от root либо с доступом к Docker. Контейнеры бота и загрузчика
всё равно работают от разных непривилегированных UID.
До переноса состояния не запускайте бота на новом сервере.

## 2. Корректно остановить старый Local Bot API

На старом сервере, в каталоге проекта:

```bash
cd /home/maks/video-services
sudo docker compose --profile bot stop bot
sudo docker compose --profile bot run --rm --no-deps -T bot python - <<'PY'
import asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from app.bot import Config
from app.runtime import local_session
async def close_old():
    cfg = Config.load()
    async with Bot(cfg.token, session=local_session(cfg.api_base, 60)) as bot:
        await bot.delete_webhook(drop_pending_updates=False)
        try:
            if not await bot.close():
                raise SystemExit('close не подтверждён; перенос пока не продолжайте.')
        except TelegramRetryAfter as exc:
            raise SystemExit(f'Повторите close через {exc.retry_after} секунд; перенос пока не продолжайте.')
asyncio.run(close_old())
PY
```

Если close завершился ошибкой, не переходите к следующему шагу. Telegram может
отказать в первые 10 минут после запуска экземпляра; подождите указанный интервал
и повторите close только при явном TelegramRetryAfter.
При сетевом тайм-ауте результат неизвестен: не повторяйте облачные/локальные запросы
выхода автоматически; разберите состояние по официальной процедуре.
Здесь используется локальный close, а не облачный logOut.
Официальная процедура: https://github.com/tdlib/telegram-bot-api#moving.

После успешного close:

```bash
sudo docker compose --profile bot stop telegram-bot-api downloader
umask 077
transfer_archive="$(sudo mktemp /tmp/video-saver-private.XXXXXXXX.tgz)"
sudo tar -czf "$transfer_archive" .env data/bot-api data/bot-state
sudo chown "$(id -u):$(id -g)" "$transfer_archive"
sudo chmod 600 "$transfer_archive"
scp -p "$transfer_archive" USER@NEW_SERVER:~/video-saver-private.tgz
```

Архив содержит секреты и состояние авторизации. Передайте его на новый сервер
через SCP/SSH; не загружайте в Git, чат или по публичной ссылке.
Старые данные сохраните до проверки нового сервера. Старого бота больше не запускайте
одновременно с новым. Исходные cookies, если нужны, перенесите отдельно через SSH.

## 3. Восстановить настройки на новом сервере

```bash
cd /opt/video-saver-bot
sudo tar -xzf "$HOME/video-saver-private.tgz"
sudo chmod 600 .env
sudo nano .env
```

Сохраните реальные TG_BOT_TOKEN, TG_ALLOWED_USER_IDS, TELEGRAM_API_ID и
TELEGRAM_API_HASH из перенесённого файла. Замените/добавьте следующие настройки
без дублирования ключей:

```dotenv
COMPOSE_FILE=compose.yaml:compose.small.yaml
MAX_UPLOAD_BYTES=250000000
DOWNLOAD_TIMEOUT=900
UPLOAD_TIMEOUT=3600
METUBE_URL=
PUID=1000
PGID=1000
DOWNLOADER_UID=65532
```

В приведённых командах используется sudo: PUID/PGID контейнера оставьте 1000,
независимо от SSH-пользователя. Это числовые UID внутри контейнеров.
При работе без sudo UID должен совпадать с владельцем данных; отдельно переназначьте
владельцев перенесённых файлов. Не назначайте боту или загрузчику UID 0.
Шаблон для чистой установки: .env.small.example.
Перенесённый маркер data/bot-state/.local-api.json нужен вместе с состоянием API:
так скрипт продолжит локальную авторизацию и не выполнит облачный logOut повторно.

## 4. Swap при 1 ГБ RAM

Если swap уже есть, сохраните его. Если нет, желательно выделить 512 МБ
и учесть это в свободном месте:

```bash
free -h
swapon --show
```

Для обычной ext4, если /swapfile ещё не существует:

```bash
sudo fallocate -l 512M /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

Добавьте один раз строку `/swapfile none swap sw 0 0` в /etc/fstab.
Если файловая система не поддерживает такой swapfile, настройте swap по её инструкции.
Swap — запас при пиках, а не обещание работы любой обработки на 1 ГБ.

## 5. Запустить только сервисы бота

```bash
cd /opt/video-saver-bot
sudo bash scripts/switch-local-api.sh
sudo docker compose --profile bot ps
sudo docker compose --profile bot logs --tail=50 bot downloader
df -h /
sudo docker stats --no-stream
```

COMPOSE_FILE из .env автоматически подключает compose.small.yaml во всех командах
и скриптах. Ограничения: бот 256 МБ, downloader 384 МБ, API 128 МБ;
каждый лимит CPU не превышает одного ядра.
MeTube имеет отдельный профиль web и в этой инструкции не запускается.
Входящие порты сервисов не публикуются; reverse proxy и TLS для них не нужны.

Если после сборки места мало, можно очистить неиспользуемый кеш сборки
командой `sudo docker builder prune`. Данные приложения и состояние API сохраняйте.
Если остаётся меньше 1,75 ГБ, запуск/скачивание потребует освободить место или
дополнительно уменьшить лимит. Не отключайте проверку диска.

## 6. Проверить перенос

Отправьте боту /help, затем ссылку на короткое видео.
Проверьте отправку в личном чате и в нужной группе через /download@M113_video_saver_bot.
В журналах должны быть polling локального API и отсутствие конфликтов второго экземпляра.
Проверьте, что запросы, исходные файлы downloader и приватные копии удаляются после обработки.
После проверки удалите временные архивы с секретами с обоих серверов.
Нагрузка двух пользователей сама по себе не гарантирует достаточность RAM:
при OOM или постоянном swap увеличьте память или уменьшите задачи.

## Запрос Codex на новом сервере

Разверни актуальный Maks113/video-saver-bot по DEPLOY-SMALL.md на этой VPS.
Ресурсы: 1 vCPU, 1 ГБ RAM, 5 ГБ свободного SSD. Используй .env.small.example /
compose.small.yaml, лимит 250 МБ, без MeTube. Сохрани перенесённые секреты и состояние
Local Bot API, подготовь владельцев через prepare-storage, проверь запас диска,
изоляцию сетей/UID, healthchecks и polling. Не выполняй облачный logOut для уже
перенесённого готового состояния и не запускай новый экземпляр до остановки старого.
