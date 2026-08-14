FROM python:3.12-slim

# ffmpeg — склейка/конвертация, aria2 — загрузки,
# deno — JS runtime для актуальных YouTube challenges в yt-dlp.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg aria2 curl unzip ca-certificates \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && deno --version \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "runner.py"]
