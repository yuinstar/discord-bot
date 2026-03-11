FROM python:3.11-slim

# FFmpeg + Node.js (yt-dlp JS 런타임용) 설치
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsodium-dev \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# yt-dlp 최신 버전으로 업데이트
RUN pip install -U yt-dlp

COPY . .
CMD ["python", "music_bot.py"]
