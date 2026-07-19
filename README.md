# Telegram Automation Framework

> Система управления пулом Telegram-аккаунтов («воркеров») под единым управлением:
> запись истории переписок, AI-обработка медиа, автоматические диалоги и интеграция
> с партнёрской CRM для оценки лидов.
>
> **Версия: v2.0.0** · **Полная спецификация: [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md)**

Этот README — точка входа: что за система, как поднять, как эксплуатировать.
Исчерпывающее описание всех подсистем, модели данных, событий, API и истории изменений —
в `docs/PROJECT_SPEC.md`. Это **единственный** источник правды по проекту; отдельные `.md`
в `docs/` не заводятся.

---

## Что делает система

Каждый воркер — это отдельный Telegram-аккаунт, который:

- подключается к Telegram через собственный SOCKS5-прокси (основной → запасной);
- пишет всю историю личных переписок в PostgreSQL;
- складывает медиафайлы в MinIO на 3 дня (метаданные и тексты — навсегда);
- транскрибирует голосовые и видеосообщения (STT через OpenRouter);
- описывает фото, стикеры, GIF, видео и документы (vision-модели через OpenRouter);
- ведёт автодиалоги (AutoChat) — инициированные переписки, генерируемые LLM,
  с имитацией живого поведения;
- оценивает собеседников как лидов и выгружает результат во внешнюю CRM.

**Область применения — только личные диалоги 1:1.** Группы и каналы не поддерживаются:
сознательное упрощение модели данных (нет таблицы `users`, в сообщениях `is_outgoing`
вместо `sender_id`). Поддержка групп потребует заметной миграции.

### Стек

| Слой | Технология |
|---|---|
| Язык / API | Python 3.12, FastAPI |
| БД | PostgreSQL 16 через `asyncpg`, без ORM — SQL руками |
| Миграции | Alembic (применяются автоматически при старте) |
| Кэш и шина событий | Redis 7 (Redis Streams) |
| Объектное хранилище | MinIO (S3-совместимое) |
| Telegram-клиент | Telethon |
| AI | OpenRouter (STT, vision, чат-модели) |
| Обработка видео | FFmpeg |
| Веб-интерфейс | React + Vite + TypeScript + Tailwind |
| Реалтайм для UI | SSE (Server-Sent Events) |
| Инфраструктура | Docker + Docker Compose |

---

## Архитектура в двух абзацах

Компоненты не вызывают друг друга напрямую — они общаются через **шину событий**
на Redis Streams, которая одновременно служит основным логом системы (архив пишется
в таблицу `events_archive`). Шина переносит только уведомления о фактах; за самими
данными модули ходят в PostgreSQL / MinIO / Telegram.

Типичная цепочка приёма сообщения: враппер получает событие из Telegram и сразу кладёт
медиа в MinIO → публикует `message.received` → модуль истории пишет в PostgreSQL и публикует
`message.saved` с флагами типов медиа → модули транскрибации и описания слушают именно
`message.saved` (к этому моменту и запись в БД, и файл в хранилище гарантированно есть)
→ результаты возвращаются на шину как `transcription.done` / `description.done`.
API транслирует шину в три SSE-потока для дашборда: статусы воркеров, события диалога,
бегущий лог. Базу UI постоянно не опрашивает.

Все фоновые сервисы поднимаются в одном процессе приложения — в `lifespan` (`api/main.py`)
как asyncio-таски, и гасятся в строго обратном порядке зависимостей.

---

## Структура репозитория

```
ProjectOutRich/
├── alembic/            ← миграции схемы БД (22 шт., линейная цепочка 0001→0022)
├── api/                ← FastAPI: main.py, sse.py, routes/ (system, events, accounts, media)
├── core/               ← общая инфраструктура: db, redis, minio, bus, config, events,
│                         event_messages, openrouter, proxy
├── modules/            ← бизнес-логика, один модуль = одна папка (включая ui_web)
├── prompts/            ← внешние markdown-промпты, читаются модулями в рантайме
├── scripts/            ← вспомогательные CLI-скрипты, запускаются вручную
├── tests/smoke/        ← 13 smoke-тестов, запуск по одному через python -m
├── docs/PROJECT_SPEC.md← единый источник правды по проекту
├── Dockerfile
├── docker-compose.yml  ← 5 сервисов, 4 volume
├── alembic.ini
├── requirements.txt
└── .env.example
```

### Модули

| Модуль | За что отвечает |
|---|---|
| `auth` | Многошаговая авторизация TG-аккаунта (start → code → опц. 2FA → success), state в Redis (TTL 15 мин), свой fingerprint на аккаунт |
| `worker` | Жизненный цикл воркера одного аккаунта (`worker.py`) и **единственная в проекте** точка общения с Telegram (`wrapper.py`) |
| `worker_manager` | Пул воркеров: start/stop/delete без перезапуска системы, статусы в Redis, pubsub для SSE |
| `history` | Единственный writer в `dialogs`/`messages`/`media`/`reactions`/`message_edits` + чистильщик MinIO + endpoint'ы истории |
| `history_sync` | «Нагон»: догружает сообщения, появившиеся пока воркер не работал (пачки по 100, обработка FloodWait) |
| `transcription` | Голос/аудио/видео/кружки → ffmpeg → STT через OpenRouter → `transcription.done` |
| `media_description` | Фото/стикеры/GIF/видео/документы → нарезка кадров ffmpeg → vision-модель → `description.done` |
| `peer_profile` | На `dialog.created` один раз дотягивает bio и день рождения через `GetFullUserRequest` |
| `autochat` | Оркестратор автопереписки: consumer шины + API создания/остановки сессий, HTML-отчёт по завершении |
| `worker_auto_runner` | Per-account автомат: расписание из N лидов в окне по МСК, расписание в БД переживает рестарт |
| `dialog_statuses` | Каталог операторских «пузырей» + единственная точка присвоения статуса диалогу |
| `lead_evaluator` | LLM-судья закрытых диалогов: вердикт + извлечённые поля, терминальный пузырь; `silence_watcher` — таймеры тишины |
| `partner_crm` | Клиент партнёрской CRM, локальный пул лидов, поллер дозалива, финализатор терминальных статусов |
| `report_delivery` | Заливка success-отчётов сервисным TG-аккаунтом в группу выгрузки «семейства» воркера |
| `ui_web` | Фронтенд-SPA (React + Vite + Tailwind + shadcn), свой Dockerfile |

---

## Быстрый старт (Windows + Docker Desktop)

### 1. Готовим `.env`

```powershell
copy .env.example .env
```

Заполнить обязательные значения (остальные имеют рабочие дефолты):

| Переменная | Зачем |
|---|---|
| `POSTGRES_PASSWORD` | пароль БД, любой; уходит в инициализацию тома postgres |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | пароль минимум 8 символов — требование MinIO, иначе контейнер не стартует |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | с https://my.telegram.org — без них не подключить ни один аккаунт |
| `OPENROUTER_API_KEY` | без него молча не работают транскрибация, описание медиа, автодиалоги и оценщик лидов |
| `CRM_BASE_URL` / `CRM_API_TOKEN` | только если используется интеграция с партнёрской CRM |
| `REPORT_GROUP_MAP` | JSON «семейство воркера → chat_id»; пусто = доставка отчётов выключена (валидное состояние) |

> `API_TOKEN` в `.env.example` присутствует как задумка, но в коде **не проверяется** —
> API не защищён токеном. Не выставляйте порт 8000 наружу без внешней авторизации.

Полный список переменных с назначением — `.env.example` и §5 спецификации.

### 2. Поднимаем контейнеры

```powershell
docker compose up -d --build
```

Первый запуск — пара минут (качаются postgres/redis/minio, собираются образы app и ui).
Миграции накатываются автоматически: `CMD` образа — `alembic upgrade head && uvicorn ...`,
поэтому упавшая миграция не даст стартовать приложению.

### 3. Проверяем, что всё живо

```powershell
docker compose ps          # postgres/redis/minio должны быть healthy
docker compose logs -f app
```

| Что | URL |
|---|---|
| Веб-интерфейс | http://localhost:3000 |
| Swagger | http://localhost:8000/docs |
| Health | http://localhost:8000/system/health |
| Stats | http://localhost:8000/system/stats |
| MinIO-консоль | http://localhost:9001 |

`/system/health` должен вернуть `status: ok` и все компоненты в `ok`.

### 4. Заводим первый аккаунт

Через UI (мастер добавления аккаунта) или из терминала:

```powershell
docker compose exec app python -m scripts.register_account
```

Скрипт — CLI-обёртка над `/auth/start` + `/auth/code`, принимает все 8 полей fingerprint'а
и опциональный `--password` для 2FA. Оба прокси проверяются до авторизации.

---

## Эксплуатация

### Деплой кода

```powershell
docker compose up -d --force-recreate --no-deps app
```

> ⚠️ **Только `--force-recreate`, не `docker compose restart app`.**
> `restart` перезапускает процесс внутри живого контейнера: grace period до SIGKILL — 10 секунд,
> и MTProto-пакет «я ухожу» не успевает дойти до Telegram. Сервер продолжает слать updates
> на мёртвое соединение, и воркеры тихо перестают получать входящие — точечный stop/start
> воркера это **не** лечит. `--force-recreate` убивает контейнер целиком, network interface
> исчезает, TCP RST приходит на транспортном уровне, и Telegram мгновенно переключается
> на новую сессию. Разбор инцидента — §20 спеки, запись от 2026-07-13 (POST-MORTEM).
>
> Диагностический сигнал: если
> `SELECT COUNT(DISTINCT account_id) FROM events_archive WHERE type='dialog.typing_observed' AND time >= NOW() - INTERVAL '30 min'`
> заметно меньше числа активных воркеров — почти наверняка эта регрессия.

### Частые команды

```powershell
# Контейнеры
docker compose ps
docker compose logs -f app
docker compose exec app bash
docker compose down                   # остановить
docker compose down -v                # остановить и стереть БД/MinIO (начать с нуля)

# Миграции
docker compose exec app alembic current
docker compose exec app alembic history
docker compose exec app alembic upgrade head
docker compose exec app alembic downgrade -1
docker compose exec app alembic revision -m "описание"

# База
docker compose exec postgres psql -U tgframework -d tgframework -c "\dt"
docker compose exec postgres psql -U tgframework -d tgframework -c "SELECT key, value FROM settings;"

# Здоровье
curl http://localhost:8000/system/health
curl http://localhost:8000/system/stats
```

### Скрипты

```powershell
docker compose exec app python -m scripts.register_account          # добавить аккаунт из терминала
docker compose exec app python -m scripts.proxy_diag                # диагностика SOCKS5-прокси
docker compose exec app python -m scripts.report_delivery_preflight # проверить группы выгрузки без отправки
docker compose exec app python -m scripts.report_delivery_selftest  # офлайн-самопроверка доставки отчётов
docker compose exec app python -m scripts.crm_probe                 # ⚠️ разведка API CRM: request-new реально забирает лиды
```

---

## Тесты

13 smoke-сьютов в `tests/smoke/`. **Это не pytest** — каждый файл самодостаточен, имеет
`_run_all()` и возвращает exit-код; запускается по одному внутри контейнера:

```powershell
docker compose exec -T app python -m tests.smoke.test_worker_supervisor
docker compose exec -T app python -m tests.smoke.test_contacted_dedup
```

Redis и MinIO не нужны (шина мокается), но пять сьютов ходят в **живую** БД
(`test_claim_attempts_and_blacklist`, `test_contacted_dedup`, `test_flood_recovery`,
`test_last_status_refresh`, `test_smart_flood_cooldown`) — они заводят и убирают за собой
sentinel-пользователей. Массового прогона одной командой не предусмотрено.

---

## Типичные проблемы

**`minio` не стартует** — `MINIO_ROOT_PASSWORD` короче 8 символов.

**`app` не может подключиться к Postgres** — пароль в `.env` не совпадает с тем, что уже
записан в volume `postgres_data`. Лечится `docker compose down -v` (сотрёт данные).

**Порт 8000 / 3000 / 9001 занят** — поменять левую часть в `ports:` в `docker-compose.yml`.

**`/system/health` возвращает `degraded`** — лёг один из компонентов, смотреть
`docker compose ps` и логи конкретного сервиса.

**Воркеры перестали получать входящие после деплоя** — почти наверняка использовали
`docker compose restart app` вместо `--force-recreate`, см. раздел «Деплой кода».

**Транскрибация/описание/автодиалоги молчат** — пустой `OPENROUTER_API_KEY` либо
несуществующее имя модели в `OPENROUTER_MODEL_*` (имена моделей сверять с каталогом
https://openrouter.ai/models).

---

## Документация

| Что | Где |
|---|---|
| Полная спецификация системы (разделы 1–19 — текущее состояние, 20 — история изменений) | [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md) |
| Известные проблемы и разбор инцидентов | §19 и §20 спецификации |
| Модель данных, шина событий, REST API | §6, §7, §18 спецификации |

**Регламент:** любое изменение в коде сразу отражается в `docs/PROJECT_SPEC.md` —
правится соответствующий раздел 1–19 и добавляется датированная запись в §20.
Отдельные `.md` в `docs/` не заводятся.
