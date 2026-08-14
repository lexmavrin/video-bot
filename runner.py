import bot


def _patched_select_video_format(info: dict, max_height: int) -> tuple[str, int]:
    """Select a real downloadable format.

    YouTube normally reports height for each format. Instagram/VK and some
    other extractors may omit height even for a valid video. In that case we
    must not discard the format just because height is unknown.
    """
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

        # Some non-YouTube extractors omit codec metadata. A normal media
        # extension with a URL is still a useful fallback candidate.
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
        # Known height wins; for unknown-height Instagram formats use bitrate.
        return (fmt.get("height") or 0, fmt.get("tbr") or fmt.get("vbr") or 0)

    candidates = [fmt for fmt in muxed if allowed(fmt)]
    candidates.sort(key=score, reverse=True)
    for fmt in candidates:
        size = bot._estimate_size(fmt, duration)
        if size is None or size <= bot.MAX_FILE_SIZE:
            actual_height = int(fmt.get("height") or max_height)
            return str(fmt["format_id"]), actual_height

    videos = [fmt for fmt in video_only if allowed(fmt)]
    videos.sort(key=score, reverse=True)
    audios = sorted(
        audio_only,
        key=lambda fmt: fmt.get("abr") or fmt.get("tbr") or 0,
        reverse=True,
    )

    for video_fmt in videos:
        for audio_fmt in audios[:8]:
            video_size = bot._estimate_size(video_fmt, duration)
            audio_size = bot._estimate_size(audio_fmt, duration)
            if (
                video_size is not None
                and audio_size is not None
                and video_size + audio_size > bot.MAX_FILE_SIZE
            ):
                continue
            actual_height = int(video_fmt.get("height") or max_height)
            return f"{video_fmt['format_id']}+{audio_fmt['format_id']}", actual_height

    # Last-resort fallback for extractors that expose a single opaque format.
    generic = [fmt for fmt in formats if fmt.get("url") and fmt.get("format_id")]
    generic.sort(key=score, reverse=True)
    for fmt in generic:
        size = bot._estimate_size(fmt, duration)
        if size is None or size <= bot.MAX_FILE_SIZE:
            return str(fmt["format_id"]), int(fmt.get("height") or max_height)

    raise RuntimeError(f"Нет доступного видеоформата до {max_height}p")


bot._select_video_format = _patched_select_video_format

if __name__ == "__main__":
    bot.main()
