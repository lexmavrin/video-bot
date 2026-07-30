import os
import re
import shutil
import asyncio
import tempfile
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import yt_dlp

# --- Настройки ---
# Токен берётся из переменной окружения BOT_TOKEN.
# Получить токен: напишите @BotFather в Telegram -> /newbot
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Лимит Telegram на отправку файла ботом ~50 МБ.
MAX_FILE_SIZE = 50 * 1024 * 1024

# Необязательный файл cookies (нужен на облаке для YouTube/Instagram).
# Укажи путь в переменной окружения COOKIES_FILE, напр. /app/cookies.txt
COOKIES_FILE = os.environ.get("COOKIES_FILE")

# Необязательный прокси (SOCKS5/HTTP) для обхода блокировок по IP.
PROXY = os.environ.get("PROXY")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+")


# --- Мини веб-сервер для Render ---
# Render требует, чтобы бесплатный сервис слушал порт из $PORT.
# Он же нужен, чтобы «будильник» (UptimeRobot) пинговал бота и тот не засыпал.
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        # UptimeRobot по умолчанию проверяет HEAD-запросом.
        # Без этого метода сервер отвечает 501, монитор считает сайт "упавшим",
        # и по факту бот всё это время не будили как надо.
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # не засоряем логи


def start_health_server():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health-сервер слушает порт %s", port)


# Если на машине есть aria2c — используем его как внешний загрузчик:
# он качает файл в несколько соединений параллельно и обычно в разы быстрее
# однопоточного скачивания yt-dlp по умолчанию.
ARIA2C_PATH = shutil.which("aria2c")


def download_video(url: str, out_dir: str) -> str:
    """Скачивает видео по ссылке и возвращает путь к файлу."""
    ydl_opts = {
        # Берём лучшее качество, влезающее в лимит; mp4 предпочтительно.
        "format": "best[filesize<50M]/best[ext=mp4]/best",
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        # Параллельное скачивание фрагментов (HLS/DASH), где применимо.
        "concurrent_fragment_downloads": 4,
    }
    if ARIA2C_PATH:
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = {
            "aria2c": ["-x", "16", "-s", "16", "-k", "1M"]
        }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
    if PROXY:
        ydl_opts["proxy"] = PROXY
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Пришли ссылку на видео из Instagram или YouTube — верну файл."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    match = URL_RE.search(text)
    if not match:
        return  # ни ссылки, ни ответа — молчим

    url = match.group(0)
    chat_id = update.effective_chat.id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
    # Мгновенная обратная связь: пользователь сразу видит, что бот взялся
    # за ссылку, а не «завис» на время скачивания.
    status_msg = await update.message.reply_text("⏳ Скачиваю видео...")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            # Скачивание блокирующее (yt-dlp), поэтому уводим его в отдельный
            # поток — event loop не подвисает и может параллельно обслуживать
            # других пользователей, пока идёт загрузка.
            path = await asyncio.to_thread(download_video, url, tmp)
        except Exception as e:
            logger.error("Ошибка скачивания: %s", e)
            await status_msg.edit_text("Не получилось скачать это видео.")
            return

        if not os.path.exists(path):
            await status_msg.edit_text("Не получилось скачать это видео.")
            return

        if os.path.getsize(path) > MAX_FILE_SIZE:
            await status_msg.edit_text(
                "Видео больше 50 МБ — Telegram не даёт боту отправить такой файл."
            )
            return

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
        # Отправляем видео без подписи и любых комментариев.
        with open(path, "rb") as f:
            await context.bot.send_video(chat_id=chat_id, video=f, supports_streaming=True)
        await status_msg.delete()


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Задайте переменную окружения BOT_TOKEN")

    start_health_server()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
