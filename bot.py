import os
import re
import tempfile
import logging
import threading
import subprocess
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

    def log_message(self, *args):
        pass  # не засоряем логи


def start_health_server():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health-сервер слушает порт %s", port)


VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}


def download_media(url: str, out_dir: str) -> list:
    """Скачивает медиа по ссылке (видео или фото) и возвращает список файлов."""
    ydl_opts = {
        # Лучшее в пределах лимита; для фото сработает запасной вариант "best".
        "format": "best[filesize<50M]/best",
        "outtmpl": os.path.join(out_dir, "%(autonumber)02d-%(id)s.%(ext)s"),
        "noplaylist": True,      # не тянуть целый плейлист (напр. с YouTube)
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "ignoreerrors": True,    # карусель: не падать из-за одного элемента
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
    if PROXY:
        ydl_opts["proxy"] = PROXY
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        logger.warning("yt-dlp не смог: %s", e)

    files = _list_files(out_dir)
    if files:
        return files

    # Фото-посты (Instagram и т.п.) yt-dlp часто не берёт — пробуем gallery-dl.
    logger.info("Пробую gallery-dl для %s", url)
    cmd = ["gallery-dl", "-q", "-D", out_dir]
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    if PROXY:
        cmd += ["--proxy", PROXY]
    cmd.append(url)
    try:
        subprocess.run(cmd, timeout=120, check=False)
    except Exception as e:
        logger.warning("gallery-dl не смог: %s", e)

    return _list_files(out_dir)


def _list_files(out_dir: str) -> list:
    """Возвращает список скачанных файлов по порядку."""
    files = [os.path.join(out_dir, f) for f in sorted(os.listdir(out_dir))]
    return [f for f in files if os.path.isfile(f)]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Пришли ссылку на видео или фото (Instagram, TikTok, YouTube, VK) — верну файл."
    )


async def send_one(context, chat_id, path):
    """Отправляет один файл в зависимости от типа (видео/фото/иное)."""
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        if ext in VIDEO_EXT:
            await context.bot.send_video(chat_id=chat_id, video=f, supports_streaming=True)
        elif ext in IMAGE_EXT:
            await context.bot.send_photo(chat_id=chat_id, photo=f)
        else:
            await context.bot.send_document(chat_id=chat_id, document=f)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    match = URL_RE.search(text)
    if not match:
        return  # ни ссылки, ни ответа — молчим

    url = match.group(0)
    chat_id = update.effective_chat.id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    with tempfile.TemporaryDirectory() as tmp:
        try:
            files = download_media(url, tmp)
        except Exception as e:
            logger.error("Ошибка скачивания: %s", e)
            files = []

        if not files:
            await update.message.reply_text("Не получилось скачать это медиа.")
            return

        sent = 0
        for path in files:
            if os.path.getsize(path) > MAX_FILE_SIZE:
                continue  # пропускаем то, что больше лимита Telegram
            try:
                await send_one(context, chat_id, path)
                sent += 1
            except Exception as e:
                logger.error("Ошибка отправки %s: %s", path, e)

        if sent == 0:
            await update.message.reply_text(
                "Файл(ы) скачались, но больше 50 МБ — Telegram не даёт боту их отправить."
            )


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
