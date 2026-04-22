# Структура проекта

> Организация файлов и папок в репозитории.
> Принцип: всё что относится к одной задаче — лежит рядом.
> Меняешь логику модуля — работаешь с его папкой, не трогаешь остальные.

---

## Структура папок

```
tg-framework/
│
├── alembic/                     ← миграции базы данных
│   ├── versions/                ← файлы миграций (создаются командой alembic revision)
│   │   └── 0001_initial.py      ← первая миграция — создаёт все таблицы и заполняет settings
│   └── env.py                   ← конфигурация Alembic, подключение к БД
│
├── core/                        ← общий код для всей системы
│   ├── db.py                    ← подключение к PostgreSQL (asyncpg)
│   ├── redis.py                 ← подключение к Redis
│   ├── minio.py                 ← подключение к MinIO
│   ├── bus.py                   ← публикация и чтение шины событий
│   └── config.py                ← загрузка .env переменных и settings из БД
│
├── modules/
│   ├── auth/                    ← авторизация аккаунтов
│   │   ├── service.py           ← логика авторизации
│   │   └── routes.py            ← API endpoints /auth/*
│   │
│   ├── worker_manager/          ← управление воркерами
│   │   ├── service.py           ← логика запуска/остановки
│   │   └── routes.py            ← API endpoints /workers/*
│   │
│   ├── worker/                  ← сам воркер
│   │   ├── worker.py            ← жизненный цикл воркера
│   │   └── wrapper.py           ← враппер Telegram (Telethon)
│   │
│   ├── history/                 ← запись истории
│   │   ├── service.py           ← логика записи сообщений
│   │   ├── cleaner.py           ← чистильщик файлов MinIO
│   │   └── routes.py            ← API endpoints /dialogs/* /messages/*
│   │
│   ├── history_sync/            ← нагон истории при старте воркера
│   │   └── service.py           ← логика синхронизации
│   │
│   ├── transcription/           ← транскрибация аудио в текст
│   │   └── service.py           ← логика транскрибации через OpenRouter
│   │
│   └── media_description/       ← описание медиа через GPT-4o
│       └── service.py           ← логика описания + нарезка кадров FFmpeg
│
├── api/
│   ├── main.py                  ← точка входа FastAPI, регистрация роутеров
│   ├── sse.py                   ← три SSE потока для дашборда
│   └── routes/
│       ├── accounts.py          ← /accounts/*
│       ├── events.py            ← /events/*
│       ├── media.py             ← /media/*
│       ├── search.py            ← /search
│       └── system.py            ← /system/*
│
├── dashboard/                   ← веб-интерфейс (позже)
│
├── docs/                        ← вся проектная документация (этот набор файлов)
│
├── alembic.ini                  ← настройки Alembic
├── docker-compose.yml           ← поднять всё окружение одной командой
├── Dockerfile                   ← сборка образа приложения
├── .env.example                 ← шаблон переменных без значений
├── .gitignore                   ← что не коммитим
└── README.md                    ← описание проекта и как запустить
```

---

## Где что менять

| Задача | Куда идти |
|---|---|
| Логика авторизации | `modules/auth/service.py` |
| Endpoint авторизации | `modules/auth/routes.py` |
| Запуск / остановка воркеров | `modules/worker_manager/service.py` |
| Жизненный цикл воркера | `modules/worker/worker.py` |
| Общение с Telegram | `modules/worker/wrapper.py` |
| Запись сообщений в БД | `modules/history/service.py` |
| Чистка файлов из MinIO | `modules/history/cleaner.py` |
| Нагон истории | `modules/history_sync/service.py` |
| Транскрибация | `modules/transcription/service.py` |
| Описание медиа / FFmpeg | `modules/media_description/service.py` |
| SSE потоки для дашборда | `api/sse.py` |
| Подключение к базе | `core/db.py` |
| Шина событий | `core/bus.py` |
| Переменные окружения | `core/config.py` |
| Миграции базы данных | `alembic/versions/` |

---

## Принципы которые мы зафиксировали

1. Каждый модуль — отдельная папка. Всё что относится к модулю лежит внутри неё
2. `core/` — только общий код который используют все модули
3. `api/` — точка входа и SSE потоки. Endpoints модулей живут в папках модулей
4. `alembic/` — все миграции базы данных. Новая миграция на каждое изменение схемы
5. `docs/` — вся проектная документация рядом с кодом
6. `.env.example` коммитим — `.env` никогда
