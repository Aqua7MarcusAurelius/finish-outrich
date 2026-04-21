# Telegram Automation Framework — Этап 1

> Этот архив содержит **инфраструктурный каркас** проекта:
> Docker-окружение, core-модули подключений (PostgreSQL / Redis / MinIO),
> Alembic с первой миграцией (все таблицы + дефолтные `settings`)
> и FastAPI с базовыми endpoint'ами `/system/health` и `/system/stats`.
>
> Полная документация проекта — отдельно в папке `docs/` (не в этом архиве).

---

## Что внутри

```
tg-framework/
├── alembic/
│   ├── versions/
│   │   └── 0001_initial.py     ← все таблицы + дефолты settings
│   ├── env.py
│   └── script.py.mako
├── alembic.ini
├── api/
│   └── main.py                  ← FastAPI: /system/health, /system/stats
├── core/
│   ├── config.py                ← загрузка .env
│   ├── db.py                    ← asyncpg pool
│   ├── redis.py                 ← async redis client
│   └── minio.py                 ← minio client + создание bucket
├── modules/                     ← пусто, заполним на следующих этапах
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── .gitignore
└── .dockerignore
```

---

## Быстрый старт (Windows + Docker Desktop)

### 1. Готовим .env

```powershell
copy .env.example .env
```

Открой `.env` в VS Code и проставь пароли:
- `POSTGRES_PASSWORD` — любой
- `MINIO_ROOT_PASSWORD` — минимум 8 символов (требование MinIO)
- `API_TOKEN` — любая случайная строка (на этом этапе не проверяется, но пусть будет)

`OPENROUTER_API_KEY` можно оставить пустым — на этапе 1 не используется.

### 2. Поднимаем контейнеры

```powershell
docker compose up -d --build
```

Первый запуск — пара минут (качается postgres/redis/minio, собирается образ приложения).

### 3. Проверяем что всё живо

Смотрим статус контейнеров:
```powershell
docker compose ps
```

Все 4 сервиса должны быть `healthy` / `running`.

Логи приложения:
```powershell
docker compose logs -f app
```

Должно увидеться что-то вроде:
```
INFO [alembic.runtime.migration] Running upgrade -> 0001, initial schema
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### 4. Проверяем API

Открой в браузере:

| Что | URL |
|---|---|
| Документация | http://localhost:8000/docs |
| Health | http://localhost:8000/system/health |
| Stats | http://localhost:8000/system/stats |
| MinIO консоль | http://localhost:9001 (логин = `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` из .env) |

`/system/health` должен вернуть:
```json
{
  "status": "ok",
  "components": {
    "postgres": "ok",
    "redis": "ok",
    "minio": "ok",
    "auth_module": "ok"
  }
}
```

### 5. Убедиться что миграция проехала

```powershell
docker compose exec postgres psql -U tgframework -d tgframework -c "\dt"
```

Должно быть 8 таблиц: `accounts`, `dialogs`, `messages`, `media`, `reactions`, `message_edits`, `settings`, `events_archive`.

```powershell
docker compose exec postgres psql -U tgframework -d tgframework -c "SELECT key, value FROM settings;"
```

Должно быть 7 дефолтных настроек.

---

## Полезные команды

```powershell
# Остановить всё
docker compose down

# Остановить и стереть данные БД / MinIO (начать с нуля)
docker compose down -v

# Пересобрать образ приложения после изменений в requirements.txt
docker compose up -d --build app

# Зайти внутрь контейнера приложения
docker compose exec app bash

# Создать новую миграцию вручную (после добавления таблицы)
docker compose exec app alembic revision -m "описание миграции"

# Применить все миграции вручную (обычно делается автоматически при старте)
docker compose exec app alembic upgrade head

# Откатить последнюю миграцию
docker compose exec app alembic downgrade -1
```

---

## Типичные проблемы

**`minio` не стартует с ошибкой про пароль**
MinIO требует `MINIO_ROOT_PASSWORD` минимум 8 символов. Проверь `.env`.

**`app` упал с ошибкой подключения к Postgres**
Docker Compose ждёт healthcheck'а — но если пароль в `.env` не совпадает с тем что уже записан в volume `postgres_data`, Postgres будет отвергать подключения. Решение: `docker compose down -v` чтобы стереть volume и начать с нуля.

**Порт 8000 / 9001 занят**
Поменяй в `docker-compose.yml` левую часть в `ports: - "8000:8000"` на свободный (например `8080:8000`).

**`/docs` открывается, но `/system/health` возвращает `degraded`**
Значит один из компонентов лёг — посмотри `docker compose ps` и `docker compose logs <сервис>`.

---

## Что дальше

После того как Этап 1 запустился и `/system/health` вернул `ok`:

- **Этап 2** — шина событий (`core/bus.py` на Redis Streams + запись в `events_archive`), SSE-инфраструктура
- **Этап 3** — враппер Telegram через Telethon, модуль авторизации, менеджер воркеров
- **Этап 4** — запись истории, нагон, чистильщик
- **Этап 5** — транскрибация (Whisper через OpenRouter) и описание медиа (GPT-4o + FFmpeg)
- **Этап 6** — оставшиеся API endpoint'ы (диалоги, сообщения, события, поиск, дашборд)

Подробный план — в основном README проекта.
