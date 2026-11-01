FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      wget curl ca-certificates fonts-liberation libnss3 libatk-bridge2.0-0 \
      libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2 \
      libdrm2 libxkbcommon0 libpango-1.0-0 libcairo2 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install chromium
COPY . .
CMD ["python", "bot.py"]
