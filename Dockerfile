FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1

# chromium + all playwright deps
RUN apt-get update && apt-get install -y --no-install-recommends \
      wget curl ca-certificates \
      fonts-liberation fonts-noto-color-emoji \
      libcups2 libxfixes3 \
      libnss3 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 \
      libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 \
      libgbm1 libasound2 libdrm2 libxkbcommon0 \
      libpango-1.0-0 libcairo2 libcairo-gobject2 \
      libxrender1 libxtst6 libxi6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install chromium \
 && playwright install-deps chromium

COPY . .
CMD ["python", "bot.py"]
