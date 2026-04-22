FROM python:3.12-slim

WORKDIR /app

# System dependencies: ffmpeg нужен модулю описания медиа (этап 5),
# curl пригодится для отладки и healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code (в dev-режиме перекрывается volume-ом из docker-compose)
COPY . .

EXPOSE 8000

# Старт: сначала миграции, потом приложение.
# Для dev с hot-reload запускать uvicorn локально вне Docker,
# либо использовать docker-compose.dev.yml с override команды.
CMD ["sh", "-c", "alembic upgrade head && uvicorn api.main:app --host 0.0.0.0 --port 8000"]
