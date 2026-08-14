import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

import bot


_original_download_video = bot.download_video


def _patched_select_video_format(info: dict, max_height: int) -> tuple[str, int]:
    """Fallback selector for Instagram/VK and other non-YouTube extractors."""
    duration = info.get("duration") or 0
    formats = info.get("formats") or []

    muxed = []
    video_only = []
    audio_only = []

    for fmt in formats:
        if not fmt.get("url"):
            continue
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        has_video = vcodec not in (None, "none")
        has_audio = acodec not in (None, "none")

        if not has_video and not has_audio and fmt.get("ext") in {"mp4", "mov", "webm", "m4v"}:
            muxed.append(fmt)
        elif has_video and has_audio:
            muxed.append(fmt)
        elif has_video:
            video_only.append(fmt)
        elif has_audio:
            audio_only.append(fmt)

    def allowed(fmt: dict) -> bool:
        height = fmt.get("height")
        return height is None or height <= max_height

    def score(fmt: dict):
        return (fmt.get("height") or 0, fmt.get("tbr") or fmt.get("vbr") or 0)

    candidates = [fmt for fmt in muxed if allowed(fmt)]
    candidates.sort(key=score, reverse=True)
    for fmt in candidates:
        size = bot._estimate_size(fmt, duration)
        if size is None or size <= bot.MAX_FILE_SIZE:
            return str(fmt["format_id"]), int(fmt.get("height") or max_height)

    videos = [fmt for fmt in video_only if allowed(fmt)]
    videos.sort(key=score, reverse=True)
    audios = sorted(audio_only, key=lambda fmt: fmt.get("abr") or fmt.get("tbr") or 0, reverse=True)

    for video_fmt in videos:
        for audio_fmt in audios[:8]:
            video_size = bot._estimate_size(video_fmt, duration)
            audio_size = bot._estimate_size(audio_fmt, duration)
            if video_size is not None and audio_size is not None and video_size + audio_size > bot.MAX_FILE_SIZE:
                continue
            return f"{video_fmt['format_id']}+{audio_fmt['format_id']}", int(video_fmt.get("height") or max_height)

    generic = [fmt for fmt in formats if fmt.get("url") and fmt.get("format_id")]
    generic.sort(key=score, reverse=True)
    for fmt in generic:
        size = bot._estimate_size(fmt, duration)
        if size is None or size <= bot.MAX_FILE_SIZE:
            return str(fmt["format_id"]), int(fmt.get("height") or max_height)

    raise RuntimeError(f"Нет доступного видеоформата до {max_height}p")


def _is_youtube(info: dict, url: str) -> bool:
    key = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    page = str(info.get("webpage_url") or url).lower()
    return "youtube" in key or "youtube.com" in page or "youtu.be" in page


def _direct_http_format(fmt: dict) -> bool:
    url = str(fmt.get("url") or "")
    protocol = str(fmt.get("protocol") or "")
    return url.startswith(("http://", "https://")) and "m3u8" not in protocol and "dash" not in protocol


def _format_size(fmt: dict, duration) -> int | None:
    return bot._estimate_size(fmt, duration)


def _download_url(fmt: dict, destination: str) -> None:
    headers = dict(fmt.get("http_headers") or {})
    headers.setdefault("User-Agent", "Mozilla/5.0")
    request = urllib.request.Request(fmt["url"], headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response, open(destination, "wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            if out.tell() > bot.MAX_FILE_SIZE + 8 * 1024 * 1024:
                raise RuntimeError("Видео больше лимита Telegram")


def _youtube_candidates(info: dict, max_height: int):
    duration = info.get("duration") or 0
    formats = [f for f in (info.get("formats") or []) if _direct_http_format(f)]

    def height_ok(f):
        h = f.get("height")
        return h is not None and 0 < h <= max_height

    def score(f):
        return (f.get("height") or 0, f.get("tbr") or f.get("vbr") or 0)

    # First: a ready-made MP4 containing both video and audio.
    muxed = [
        f for f in formats
        if height_ok(f)
        and f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
    ]
    muxed.sort(key=lambda f: (f.get("ext") == "mp4", *score(f)), reverse=True)
    for f in muxed:
        size = _format_size(f, duration)
        if size is None or size <= bot.MAX_FILE_SIZE:
            yield (f, None, int(f.get("height") or max_height))

    # Then: separate streams. Prefer AVC/MP4 + M4A for Telegram compatibility.
    videos = [
        f for f in formats
        if height_ok(f)
        and f.get("vcodec") not in (None, "none")
        and f.get("acodec") in (None, "none")
    ]
    audios = [
        f for f in formats
        if f.get("vcodec") in (None, "none")
        and f.get("acodec") not in (None, "none")
    ]
    videos.sort(
        key=lambda f: (
            str(f.get("vcodec") or "").startswith("avc1"),
            f.get("ext") == "mp4",
            *score(f),
        ),
        reverse=True,
    )
    audios.sort(
        key=lambda f: (
            f.get("ext") in {"m4a", "mp4"},
            str(f.get("acodec") or "").startswith("mp4a"),
            f.get("abr") or f.get("tbr") or 0,
        ),
        reverse=True,
    )

    for v in videos:
        for a in audios[:6]:
            vs = _format_size(v, duration)
            aus = _format_size(a, duration)
            if vs is not None and aus is not None and vs + aus > bot.MAX_FILE_SIZE:
                continue
            yield (v, a, int(v.get("height") or max_height))


def _download_youtube(url: str, out_dir: str, requested_height: int | None) -> tuple[str, int]:
    info = bot._extract_info(url)
    ladder = [1080, 720, 480, 360]
    if requested_height:
        ladder = [h for h in ladder if h <= requested_height]
        if requested_height not in ladder:
            ladder.insert(0, requested_height)

    last_error = None
    for height in ladder:
        tried = 0
        for video_fmt, audio_fmt, actual_height in _youtube_candidates(info, height):
            tried += 1
            if tried > 8:
                break
            bot.clear_dir(out_dir)
            try:
                if audio_fmt is None:
                    ext = video_fmt.get("ext") or "mp4"
                    path = os.path.join(out_dir, f"youtube-{info.get('id', 'video')}.{ext}")
                    bot.logger.info("YouTube direct muxed: format=%s height=%s", video_fmt.get("format_id"), actual_height)
                    _download_url(video_fmt, path)
                else:
                    video_ext = video_fmt.get("ext") or "mp4"
                    audio_ext = audio_fmt.get("ext") or "m4a"
                    video_path = os.path.join(out_dir, f"video.{video_ext}")
                    audio_path = os.path.join(out_dir, f"audio.{audio_ext}")
                    path = os.path.join(out_dir, f"youtube-{info.get('id', 'video')}.mp4")
                    bot.logger.info(
                        "YouTube direct pair: video=%s audio=%s height=%s",
                        video_fmt.get("format_id"), audio_fmt.get("format_id"), actual_height,
                    )
                    _download_url(video_fmt, video_path)
                    _download_url(audio_fmt, audio_path)
                    subprocess.run(
                        ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-i", audio_path,
                         "-c", "copy", "-movflags", "+faststart", path],
                        check=True,
                        timeout=120,
                    )

                if not os.path.exists(path):
                    raise RuntimeError("Не удалось создать видеофайл")
                if os.path.getsize(path) > bot.MAX_FILE_SIZE:
                    raise RuntimeError("Видео больше лимита Telegram")
                return path, actual_height
            except Exception as exc:
                last_error = exc
                bot.logger.info("YouTube direct candidate failed: %s", exc)

        if tried == 0:
            last_error = RuntimeError(f"Нет прямого YouTube-формата до {height}p")

    raise last_error or RuntimeError("Не удалось скачать YouTube видео")


def _patched_download_video(url: str, out_dir: str, requested_height: int | None):
    # Detect YouTube once. Other sites keep the already-working code path.
    info = bot._extract_info(url)
    if _is_youtube(info, url):
        # Reuse the same idea, but _download_youtube performs its own extraction so
        # signed URLs are as fresh as possible immediately before download.
        return _download_youtube(url, out_dir, requested_height)
    return _original_download_video(url, out_dir, requested_height)


bot._select_video_format = _patched_select_video_format
bot.download_video = _patched_download_video

if __name__ == "__main__":
    bot.main()
