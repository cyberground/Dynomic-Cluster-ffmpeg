FROM python:3.11-slim

# System-Dependencies: ffmpeg + Node.js (fuer yt-dlp JS Runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Verify installations
RUN echo "=== Build Verification ===" \
    && python3 --version \
    && node --version \
    && ffmpeg -version 2>&1 | head -1 \
    && yt-dlp --version \
    && echo "=== All OK ==="

COPY app.py .

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
