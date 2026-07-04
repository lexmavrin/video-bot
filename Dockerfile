FROM python:3.12-slim

# ffmpeg нужен yt-dlp для склейки видео и аудио
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
# Если используешь cookies — положи cookies.txt рядом, он скопируется в образ:
COPY . .

CMD ["python", "bot.py"]
