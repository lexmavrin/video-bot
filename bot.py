import os
import re
import shutil
import asyncio
import tempfile
import logging
import threading
import subprocess
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterable

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import yt_dlp


# --- Настройки ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
COOKIES_FILE = os.environ.get("COOKIES_FILE")
PROXY = os.environ.get("PROXY")

# Bot API принимает до 50 МБ; оставляем запас под multipart/служебные данные.
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "49"))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

# На слабом Render по умолчанию выполняем только одну тяжёлую загрузку за раз.
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "1")))
MAX_GALLERY_ITEMS = max(1, int(os.environ.get("MAX_GALLERY_ITEMS", "10")))
REQUEST_TTL_SECONDS = 30 * 60

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# python-telegram-bot использует httpx. На INFO он пишет полный Bot API URL,
# в котором находится токен. Не допускаем появления токена в Render Logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXTS = {".mp3", ".m4a"}

ARIA2C_PATH = shutil.which("aria2c")
GALLERY_DL_PATH = shutil.which("gallery-dl")


def log_cookie_diagnostics() -> None:
    """Логирует только безопасные метаданные cookie-файла, без значений cookies."""
    if not COOKIES_FILE:
        logger.warning("Cookies diagnostics: COOKIES_FILE is not set")
        return

    if not os.path.exists(COOKIES_FILE):
        logger.warning("Cookies diagnostics: file not found at %s", COOKIES_FILE)
        return

    try:
        size = os.path.getsize(COOKIES_FILE)
        youtube_rows = 0
        google_rows = 0
        total_cookie_rows = 0
        with open(COOKIES_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                total_cookie_rows += 1
                lower = stripped.lower()
                if "youtube.com" in lower or "youtu.be" in lower:
                    youtube_rows += 1
                if "google.com" in lower:
                    google_rows += 1
        logger.info(
            "Cookies diagnostics: found=yes size=%s bytes rows=%s youtube_rows=%s google_rows=%s",
            size,
            total_cookie_rows,
            youtube_rows,
            google_rows,
        )
    except Exception as exc:
        logger.warning("Cookies diagnostics: could not inspect file: %s", exc)


# --- Health endpoint для Render ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_health_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health-сервер слушает порт %s", port)


# --- Очередь и короткоживущие ссылки для inline-кнопок ---
@dataclass
class PendingRequest:
    url: str
    user_id: int
    created_at: float


class DownloadQueue:
    def __init__(self, concurrency: int):
        self.semaphore = asyncio.Semaphore(concurrency)
        self._waiting = 0
        self._active = 0
        self._lock = asyncio.Lock()

    async def enter(self) -> int:
        async with self._lock:
            position = self._active + self._waiting + 1
            self._waiting += 1
        await self.semaphore.acquire()
        async with self._lock:
            self._waiting -= 1
            self._active += 1
        return position

    async def leave(self) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)
        self.semaphore.release()


DOWNLOAD_QUEUE = DownloadQueue(MAX_CONCURRENT_DOWNLOADS)
PENDING: dict[str, PendingRequest] = {}


def cleanup_pending() -> None:
    cutoff = time.time() - REQUEST_TTL_SECONDS
    expired = [key for key, req in PENDING.items() if req.created_at < cutoff]
    for key in expired:
        PENDING.pop(key, None)


def register_request(url: str, user_id: int) -> str:
    cleanup_pending()
    token = uuid.uuid4().hex[:10]
    PENDING[token] = PendingRequest(url=url, user_id=user_id, created_at=time.time())
    return token


# --- Скачивание ---
def common_ydl_opts(out_dir: str) -> dict:
    opts = {
        "outtmpl": os.path.join(out_dir, "%(title).80s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "concurrent_fragment_downloads": 2,
        "retries": 3,
        "fragment_retries": 3,
    }
    if ARIA2C_PATH:
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {
            "aria2c": ["-x", "4", "-s", "4", "-k", "1M"]
        }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    if PROXY:
        opts["proxy"] = PROXY
    return opts


def media_files(out_dir: str) -> list[str]:
    files = []
    for path in Path(out_dir).rglob("*"):
        if path.is_file() and not path.name.endswith((".part", ".ytdl")):
            files.append(str(path))
    return sorted(files, key=lambda p: os.path.getmtime(p))


def clear_dir(out_dir: str) -> None:
    for path in Path(out_dir).iterdir():
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                path.unlink()
            except OSError:
                pass


def _quality_format(height: int) -> str:
    limit = MAX_FILE_SIZE_MB
    return (
        f"best[height<={height}][ext=mp4][filesize<{limit}M]/"
        f"best[height<={height}][ext=mp4][filesize_approx<{limit}M]/"
        f"bestvideo[height<={height}][ext=mp4][filesize<{limit}M]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={height}][ext=mp4][filesize_approx<{limit}M]+bestaudio[ext=m4a]/"
        f"best[height<={height}]/best"
    )


def download_video(url: str, out_dir: str, requested_height: int | None) -> tuple[str, int]:
    ladder = [1080, 720, 480, 360]
    if requested_height:
        ladder = [h for h in ladder if h <= requested_height]
        if requested_height not in ladder:
            ladder.insert(0, requested_height)

    last_error: Exception | None = None
    for height in ladder:
        clear_dir(out_dir)
        opts = common_ydl_opts(out_dir)
        opts.update({
            "format": _quality_format(height),
            "merge_output_format": "mp4",
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
            candidates = [p for p in media_files(out_dir) if Path(p).suffix.lower() in VIDEO_EXTS]
            if not candidates:
                raise RuntimeError("yt-dlp не создал видеофайл")
            path = max(candidates, key=os.path.getsize)
            if os.path.getsize(path) <= MAX_FILE_SIZE:
                return path, height
            logger.info("Видео %sp слишком большое (%s байт), пробуем ниже", height, os.path.getsize(path))
        except Exception as exc:
            last_error = exc
            logger.info("Не удалось скачать %sp: %s", height, exc)

    if last_error:
        raise last_error
    raise RuntimeError("Не удалось подобрать видео под лимит Telegram")


def download_audio(url: str, out_dir: str) -> str:
    clear_dir(out_dir)
    opts = common_ydl_opts(out_dir)
    opts.update({
        "format": f"bestaudio[filesize<{MAX_FILE_SIZE_MB}M]/bestaudio[filesize_approx<{MAX_FILE_SIZE_MB}M]/bestaudio",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    candidates = [p for p in media_files(out_dir) if Path(p).suffix.lower() in AUDIO_EXTS]
    if not candidates:
        raise RuntimeError("Не удалось создать аудиофайл")
    path = max(candidates, key=os.path.getsize)
    if os.path.getsize(path) > MAX_FILE_SIZE:
        raise RuntimeError("Аудио больше лимита Telegram")
    return path


def download_gallery(url: str, out_dir: str) -> list[str]:
    if not GALLERY_DL_PATH:
        raise RuntimeError("gallery-dl не найден")
    clear_dir(out_dir)
    cmd = [GALLERY_DL_PATH, "--no-input", "-q", "-D", out_dir]
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    if PROXY:
        cmd += ["--proxy", PROXY]
    cmd.append(url)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "gallery-dl error").strip()
        raise RuntimeError(detail[-1000:])

    files = [p for p in media_files(out_dir) if Path(p).suffix.lower() in PHOTO_EXTS | VIDEO_EXTS]
    files = [p for p in files if os.path.getsize(p) <= MAX_FILE_SIZE]
    if not files:
        raise RuntimeError("В публикации не найдено подходящих фото/видео")
    return files[:MAX_GALLERY_ITEMS]


# --- Telegram UI ---
def chooser_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Лучшее", callback_data=f"dl:{token}:best"),
            InlineKeyboardButton("🎬 1080p", callback_data=f"dl:{token}:1080"),
        ],
        [
            InlineKeyboardButton("🎬 720p", callback_data=f"dl:{token}:720"),
            InlineKeyboardButton("🎬 480p", callback_data=f"dl:{token}:480"),
        ],
        [InlineKeyboardButton("🎵 Аудио", callback_data=f"dl:{token}:audio")],
        [InlineKeyboardButton("🖼 Фото / карусель", callback_data=f"dl:{token}:gallery")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Пришли ссылку на публикацию или видео. Я могу вернуть видео, аудио, фото или карусель."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    match = URL_RE.search(text)
    if not match:
        return

    url = match.group(0).rstrip(".,;:!?)\"]}")
    token = register_request(url, update.effective_user.id)
    await update.message.reply_text(
        "Что скачать?",
        reply_markup=chooser_keyboard(token),
        disable_web_page_preview=True,
    )


async def send_gallery(context: ContextTypes.DEFAULT_TYPE, chat_id: int, paths: Iterable[str]) -> None:
    paths = list(paths)
    if len(paths) == 1:
        path = paths[0]
        ext = Path(path).suffix.lower()
        with open(path, "rb") as f:
            if ext in PHOTO_EXTS:
                await context.bot.send_photo(chat_id=chat_id, photo=f)
            elif ext == ".mp4":
                await context.bot.send_video(chat_id=chat_id, video=f, supports_streaming=True)
            else:
                await context.bot.send_document(chat_id=chat_id, document=f)
        return

    with ExitStack() as stack:
        media = []
        for path in paths[:10]:
            fh = stack.enter_context(open(path, "rb"))
            ext = Path(path).suffix.lower()
            if ext in PHOTO_EXTS:
                media.append(InputMediaPhoto(media=fh))
            elif ext == ".mp4":
                media.append(InputMediaVideo(media=fh, supports_streaming=True))
            else:
                continue
        if len(media) >= 2:
            await context.bot.send_media_group(chat_id=chat_id, media=media)
        elif len(media) == 1:
            item_path = next(p for p in paths if Path(p).suffix.lower() in PHOTO_EXTS | {".mp4"})
            await send_gallery(context, chat_id, [item_path])


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        _, token, action = query.data.split(":", 2)
    except ValueError:
        await query.edit_message_text("Эта кнопка устарела. Пришли ссылку ещё раз.")
        return

    req = PENDING.get(token)
    if not req or time.time() - req.created_at > REQUEST_TTL_SECONDS:
        PENDING.pop(token, None)
        await query.edit_message_text("Ссылка устарела. Пришли её ещё раз.")
        return
    if req.user_id != update.effective_user.id:
        await query.answer("Эта кнопка относится к ссылке другого пользователя.", show_alert=True)
        return

    PENDING.pop(token, None)
    chat_id = update.effective_chat.id

    status_by_action = {
        "audio": "⏳ Готовлю аудио…",
        "gallery": "⏳ Скачиваю фото / карусель…",
        "best": "⏳ Скачиваю видео…",
        "1080": "⏳ Скачиваю 1080p…",
        "720": "⏳ Скачиваю 720p…",
        "480": "⏳ Скачиваю 480p…",
    }
    await query.edit_message_text(status_by_action.get(action, "⏳ Скачиваю…"))

    entered = False
    try:
        position = await DOWNLOAD_QUEUE.enter()
        entered = True
        if position > MAX_CONCURRENT_DOWNLOADS:
            await query.edit_message_text(f"⏳ В очереди: примерно №{position}. Начну автоматически.")
            await query.edit_message_text(status_by_action.get(action, "⏳ Скачиваю…"))

        with tempfile.TemporaryDirectory() as tmp:
            if action == "gallery":
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
                paths = await asyncio.to_thread(download_gallery, req.url, tmp)
                await send_gallery(context, chat_id, paths)

            elif action == "audio":
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                path = await asyncio.to_thread(download_audio, req.url, tmp)
                with open(path, "rb") as f:
                    await context.bot.send_audio(chat_id=chat_id, audio=f)

            else:
                requested_height = None if action == "best" else int(action)
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
                path, actual_height = await asyncio.to_thread(download_video, req.url, tmp, requested_height)
                with open(path, "rb") as f:
                    if Path(path).suffix.lower() == ".mp4":
                        await context.bot.send_video(chat_id=chat_id, video=f, supports_streaming=True)
                    else:
                        await context.bot.send_document(chat_id=chat_id, document=f)
                if requested_height and actual_height < requested_height:
                    logger.info("Для %sp автоматически выбран %sp из-за лимита размера", requested_height, actual_height)

        await query.delete_message()

    except subprocess.TimeoutExpired:
        await query.edit_message_text("Не получилось: сайт слишком долго отвечал. Попробуй ещё раз.")
    except Exception as exc:
        logger.exception("Ошибка обработки %s: %s", req.url, exc)
        message = str(exc).lower()
        if "larger" in message or "лимит" in message or "too large" in message:
            text = "Не удалось подобрать вариант меньше 50 МБ. Попробуй 480p или аудио."
        else:
            text = "Не получилось скачать этот контент. Попробуй другой вариант или ссылку."
        await query.edit_message_text(text)
    finally:
        if entered:
            await DOWNLOAD_QUEUE.leave()


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Задайте переменную окружения BOT_TOKEN")

    start_health_server()
    log_cookie_diagnostics()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_choice, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(
        "Бот запущен: max_file=%sMB, concurrent=%s",
        MAX_FILE_SIZE_MB,
        MAX_CONCURRENT_DOWNLOADS,
    )
    app.run_polling()


if __name__ == "__main__":
    main()
