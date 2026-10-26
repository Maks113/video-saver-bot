# Video Saver Bot + MeTube

Telegram-бот скачивает публичные видео из Instagram, YouTube, VK Видео и TikTok.
Качество до 720p, итоговый файл — не больше **50 000 000 байт**.
Для больших видео используется отдельный веб-интерфейс MeTube.
Оба приложения работают в Docker Compose. Local Telegram Bot API Server не нужен.

## Запуск

Нужны Linux, Docker Engine и плагин Docker Compose.

```bash
git clone https://github.com/Maks113/video-saver-bot.git
cd video-saver-bot
cp .env.example .env
chmod 600 .env
mkdir -p data/metube/downloads data/metube/state cookies
```

В `.env` задайте `TG_BOT_TOKEN` от BotFather и `TG_ALLOWED_USER_IDS` (числовые ID через запятую).
Без токена и списка разрешённых пользователей бот не запускается.

Для доступа к MeTube из домашней сети задайте LAN-адрес сервера в `METUBE_BIND_IP` и `METUBE_URL`,
например `192.168.1.41` и `http://192.168.1.41:8081`. По умолчанию интерфейс доступен только локально.
Интерфейс не защищён паролем: не публикуйте порт в интернете. Для удалённого доступа используйте VPN
или reverse proxy с авторизацией.

```bash
docker compose up -d metube
docker compose --profile bot up -d --build
docker compose --profile bot ps
```

На Ubuntu без Docker: `sudo bash scripts/install-server.sh`.
Скрипт устанавливает Docker/Compose из репозиториев Ubuntu, включает автозапуск и запускает MeTube.
Бот запускается, если токен уже указан в `.env`. При отсутствии доступа к Docker используйте `sudo docker compose ...`.

## Использование

- Пришлите одну ссылку в личный чат. Доступ разрешён только указанным Telegram ID.
- `/start`, `/help` — справка; `/metube` — ссылка на MeTube; `/id` — собственный ID.
- Одновременно скачивается одно видео. У пользователя может быть одна задача.
- Бот не пережимает большие видео и не передаёт их в MeTube автоматически: вставьте ссылку в MeTube самостоятельно.
- Прямые трансляции и целые плейлисты ботом не скачиваются. Для публикаций с несколькими видео берётся первый элемент.
- Временные файлы бота удаляются после отправки, ошибок и при остановке контейнера.
- Очередь хранится в памяти. После перезапуска ссылки нужно прислать заново.

MeTube сохраняет видео в `data/metube/downloads`, настройки и историю — в `data/metube/state`.
Они сохраняются после перезапуска контейнеров. Автоматического удаления скачанного через MeTube нет.

## Cookies

Некоторые ссылки требуют авторизации; площадки могут ограничивать IP сервера.
Работу конкретного видео нужно проверять отдельно.

Для бота положите Netscape cookie-файлы в `cookies/youtube.txt`, `cookies/instagram.txt`,
`cookies/vk.txt` или `cookies/tiktok.txt`. Исходники подключены только для чтения;
бот использует временную копию. Для MeTube cookies загружаются отдельно через его интерфейс.

`.env`, cookies, скачанные файлы и состояние исключены из Git и контекста сборки Docker.
Не добавляйте их в репозиторий вручную.

## Обслуживание

```bash
docker compose --profile bot logs --tail=100 -f
docker compose --profile bot restart bot
docker compose --profile bot down
```

После изменения `.env` примените настройки через `docker compose --profile bot up -d`, а не `restart`.
Версии MeTube и yt-dlp закреплены. Для обновления измените тег в `compose.yaml` и версию в `requirements.txt`,
затем пересоберите и пересоздайте сервисы. Проверяйте совместимость состояния при смене версий.

## Проверки

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
docker compose --env-file .env.example config --quiet
docker build -t video-saver-bot:test .
```

GitHub Actions выполняет тесты, проверку Compose и сборку образа.
Тесты проверяют доступ, размер файлов, тайм-ауты, остановку процессов и очистку.
Тесты не отправляют сообщения в Telegram.
