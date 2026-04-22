# Отчёт по Этапам 1 и 2

> Проект: `finish-outrich` — Telegram Automation Framework
> Репо: https://github.com/Aqua7MarcusAurelius/finish-outrich
> Финальный коммит сессии: `a0aedca` (`Stage 1 + 2: infra, migrations, event bus, SSE`)
> Сессия: ~4 часа, 21 апреля 2026

---

## Что сделали

### Этап 1 — Инфраструктура

**Цель:** поднять Docker-окружение, подключения к БД и хранилищам, базовый FastAPI с health-check'ами.

**Компоненты:**

| Сервис | Роль | Порт |
|---|---|---|
| PostgreSQL 16 | Основная БД | 5432 (только внутри сети) |
| Redis 7 | Шина событий + кэш | 6379 (только внутри сети) |
| MinIO | Объектное хранилище | 9000 (S3 API, внутри), 9001 (консоль) |
| FastAPI (Python 3.12) | API | 8000 |

**Написаны:**

- `docker-compose.yml` + `Dockerfile` — все 4 сервиса в одном стеке, с healthcheck'ами и volumes для персистентности
- `core/config.py` — загрузка `.env` через `pydantic-settings`
- `core/db.py` — пул подключений `asyncpg` (ORM не используем, SQL руками)
- `core/redis.py` — async-клиент `redis.asyncio`
- `core/minio.py` — синхронный `minio-py` обёрнутый в `asyncio.to_thread`, auto-создание bucket на старте
- `alembic/env.py` + `alembic/versions/0001_initial.py` — первая миграция: 8 таблиц (`accounts`, `dialogs`, `messages`, `media`, `reactions`, `message_edits`, `settings`, `events_archive`) + 7 дефолтных записей в `settings`
- `api/main.py` — точка входа FastAPI с lifespan-хуком для инициализации подключений, endpoint'ы `/system/health` и `/system/stats`

**Проверки выполнены:**

- Все 4 контейнера `healthy`
- Миграция накатывается автоматически при старте (через `alembic upgrade head` в `CMD`)
- `/system/health` возвращает `ok` по всем компонентам
- В MinIO через консоль виден созданный bucket `tgframework`
- В PostgreSQL через `psql` видны все 8 таблиц и 7 настроек

---

### Этап 2 — Шина событий

**Цель:** реализовать центральный event bus на Redis Streams, параллельное архивирование в PostgreSQL, SSE-поток для будущего дашборда.

**Написаны:**

- `core/events.py` — константы: `Module` (кто публикует), `Status` (результат), `EventType` (список всех типов событий проекта)
- `core/event_messages.py` — справочник шаблонов для поля `message`, которое вычисляется на лету при отдаче через API (не хранится в БД — можно менять формулировки без миграций)
- `core/bus.py` — ядро шины:
  - `publish()` — публикация события (XADD в Redis Stream + hex UUID как `id`)
  - `read_live()` — чтение для SSE-подписчиков (XREAD по `last_id`, без consumer group)
  - `archive_writer_loop()` — фоновая задача: читает Stream через consumer group `archive-writer`, батчами INSERT'ит в `events_archive`, делает XACK
  - Stream ограничен `maxlen=10_000` с `approximate=True` — хвост обрезается автоматически, архив всё равно в БД
- `api/sse.py` — утилиты Server-Sent Events: `sse_format()` и `sse_heartbeat()`
- `api/routes/events.py` — endpoint'ы:
  - `GET /events` — архив с фильтрами (`account_id`, `module`, `type`, `status`, `parent_id`, `from`, `to`) и курсорной пагинацией (base64 от `(time, id)`)
  - `GET /events?root_id=...` — отдельный режим: рекурсивный CTE для получения всей цепочки событий от корня
  - `GET /events/stream` — живой SSE-поток с поддержкой `Last-Event-ID` для переподключений, heartbeat каждые ~30 сек
  - `GET /events/{event_id}` — одно событие по id
- `api/main.py` — обновлён:
  - В `lifespan` стартует фоновая задача `archive_writer_loop`
  - При shutdown корректно гасит её через `task.cancel()`
  - Добавлен debug-endpoint `POST /system/_debug/emit-event` (только при `APP_ENV=development`) для ручной публикации тестовых событий
  - К CORS добавлен заголовок `Last-Event-ID`
- `alembic/versions/0002_drop_events_account_fk.py` — миграция удаления FK `events_archive.account_id → accounts.id` (см. раздел о багах ниже)

**Архитектура потока события:**

```
модуль вызывает bus.publish(...)
     │
     ▼
 XADD в Redis Stream "events:stream" (maxlen ~10000)
     │
     ├──▶ archive_writer_loop (consumer group "archive-writer")
     │       │
     │       └──▶ INSERT в events_archive (PostgreSQL)
     │
     └──▶ SSE-подписчики /events/stream
             └── XREAD по last_id → отдаёт клиенту в реальном времени
```

**Проверки выполнены:**

- `POST /system/_debug/emit-event` возвращает созданное событие с `id` (32-hex UUID)
- `GET /events` показывает его в архиве с вычисленным полем `message`
- Открытый в браузере `/events/stream` ловит heartbeat'ы и новые события появляются в ту же секунду как только эмитнуты через debug endpoint
- Интеграционный тест на стороне разработки прогнан: 3 связанных события (parent_id цепочкой) — все корректно архивированы, рекурсивный CTE возвращает полную цепочку

---

## Баги которые поймали и починили

### Баг №1. FK `events_archive.account_id → accounts.id`

**Как проявился:** при интеграционном тесте шины события с `account_id=42` не записывались в архив:
```
asyncpg.exceptions.ForeignKeyViolationError:
  insert or update on table "events_archive" violates foreign key constraint
  "events_archive_account_id_fkey"
  DETAIL: Key (account_id)=(42) is not present in table "accounts".
```

**В чём проблема:** в миграции `0001_initial.py` я объявил колонку `events_archive.account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL`. Но события жизненного цикла аккаунтов (`account.created`, `worker.started`, `system.error` с ссылкой на аккаунт) в принципе могут приходить **до** того как аккаунт зафиксирован в `accounts` — либо в рамках той же транзакции создания, либо в тестах без аккаунта вообще. FK блокировал такие вставки, и archive_writer падал.

**Когда бы выстрелило в проде:** при первой же успешной авторизации через модуль `auth` (Этап 3) — событие `account.created` эмитится **до** коммита строки в `accounts`.

**Как починили:** миграция `0002_drop_events_account_fk.py` — `DROP CONSTRAINT events_archive_account_id_fkey`. Колонка `account_id` осталась как обычный `INTEGER`, без FK. Архив событий — журнал, не операционные данные, нормально что в нём бывают ссылки на уже удалённые сущности или на сущности которые ещё только создаются.

**Урок:** FK в архивных/журнальных таблицах — частая ошибка. Для них лучше хранить ссылку как обычный `BIGINT`/`INTEGER` с индексом — без жёстких констрейнтов.

---

### Баг №2. Порядок роутов — `/events/{event_id}` vs `/events/stream`

**Как проявился:** открытие `http://localhost:8000/events/stream` в браузере возвращало:
```json
{ "detail": { "error": { "code": "EVENT_NOT_FOUND" } } }
```

**В чём проблема:** в `api/routes/events.py` я объявил роуты в порядке `/events` → `/events/{event_id}` → `/events/stream`. FastAPI (как и Starlette под ним) матчит роуты **сверху вниз**. Когда пришёл запрос `/events/stream`, он сначала проверился против `/events/{event_id}` — и заматчился, взяв `event_id="stream"`. Пошёл в Postgres искать событие с id `'stream'`, не нашёл, вернул 404.

**Когда бы выстрелило в проде:** при первой же попытке открыть живой стрим (то есть сразу, на первом скриншоте).

**Как починили:** переставил `/events/stream` **до** `/events/{event_id}` в файле. FastAPI теперь матчит статичный путь первым, и только если не совпало — идёт в динамический. Добавил комментарий в код чтобы следующий человек не поменял порядок обратно.

**Урок:** в FastAPI/Starlette всегда **статичные роуты раньше динамических**, если они живут под одним префиксом. Это не баг фреймворка, а особенность упорядоченного матчинга.

---

### Мелочи которые тоже были

- **Пароли в `.env`.** Docker Compose при первом запуске зафиксировал пароль в volume Postgres. Попытка поменять пароль в `.env` и `docker compose up` → `password authentication failed`. Лечится `docker volume rm` и пересозданием volume'а (данных ещё не было). На будущее: **заполнить `.env` с нормальными паролями ДО первого `up`**, менять потом проблематично.

- **MinIO требует пароль root минимум 8 символов.** Дефолтные `change_me_minio` в моём шаблоне пришлось менять — вылезло только при первом логине в консоль с сообщением `invalid login`.

- **Лишняя папка `{alembic`** при распаковке zip на Windows. Артефакт архиватора (возможно с бэктиком в имени `\`{alembic`). На работу не влияла, но удалили для чистоты. Убралась через `Remove-Item -Recurse -Force .\```{alembic`.

---

## Что в итоге на GitHub

Структура репо `finish-outrich` (коммит `a0aedca`):

```
finish-outrich/
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       ├── 0001_initial.py                      — 8 таблиц + дефолты settings
│       └── 0002_drop_events_account_fk.py       — фикс FK бага
├── api/
│   ├── __init__.py
│   ├── main.py                                  — FastAPI + lifespan + archive_writer
│   ├── sse.py                                   — утилиты SSE
│   └── routes/
│       ├── __init__.py
│       └── events.py                            — /events, /events/stream, /events/{id}
├── core/
│   ├── __init__.py
│   ├── config.py                                — pydantic-settings
│   ├── db.py                                    — asyncpg pool
│   ├── redis.py                                 — async redis client
│   ├── minio.py                                 — minio + auto-bucket
│   ├── events.py                                — Module, Status, EventType
│   ├── event_messages.py                        — справочник шаблонов
│   └── bus.py                                   — ядро шины
├── modules/                                     — пусто, заполнится с Этапа 3
├── alembic.ini
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── .dockerignore
├── .gitignore
└── README.md
```

**Версия приложения:** `0.2.0`
**Endpoint'ов работает:** 6 (`/system/health`, `/system/stats`, `/system/_debug/emit-event`, `/events`, `/events/{id}`, `/events/stream`)
**Endpoint'ов от финального MVP:** ~18% (6 из 33)

---

## Как поднять проект с нуля

На случай если машина поменяется или volume'ы сломаются:

```powershell
# 1. Клон репо
git clone https://github.com/Aqua7MarcusAurelius/finish-outrich.git
cd finish-outrich

# 2. Подготовка .env (ВАЖНО: сразу с нормальными паролями!)
copy .env.example .env
# Открыть .env, проставить:
#   POSTGRES_PASSWORD=<любой>
#   MINIO_ROOT_PASSWORD=<минимум 8 символов>
#   API_TOKEN=<любая случайная строка>

# 3. Поднять
docker compose up -d --build

# 4. Проверить
docker compose ps                              # все 4 healthy/running
curl http://localhost:8000/system/health       # "status": "ok"
```

**Проверка шины:**
- Swagger: http://localhost:8000/docs
- Стрим: http://localhost:8000/events/stream (в новой вкладке — повиснет, это норма)
- Эмитнуть событие: в Swagger `POST /system/_debug/emit-event` → появится в стриме

---

## Что дальше — План на Этап 3

**Цель:** Telegram-часть. Чтобы в конце этапа живой Telegram-аккаунт был подключён, воркер слушал, а при получении сообщения в шине проехала цепочка `message.received`.

**Модули к написанию:**

1. `modules/worker/wrapper.py` — враппер Telethon
   - Единственная точка общения с Telegram
   - Команды: `send_message`, `read_message`, `get_dialogs`, `get_history`
   - Управление прокси: основной → при недоступности запасной → при недоступности обоих стоп + `system.error`
   - Определение протухшей сессии (AuthKeyError, SessionExpiredError) → `account.session_expired`

2. `modules/auth/` — авторизация
   - Многошаговый флоу: start → code → (опц.) 2fa → success
   - Состояние в Redis с TTL 15 мин (ключ `auth_session:{session_id}`)
   - `TelegramClient` держится в памяти процесса авторизации
   - Endpoint'ы `/auth/start`, `/auth/code`, `/auth/2fa`, `/auth/status/{id}`, `/auth/{id}` (cancel), `/auth/reauth`
   - Предварительная проверка обоих прокси до начала флоу
   - Запись сессии в `accounts.session_data` (bytea)

3. `modules/worker_manager/` — оркестратор воркеров
   - Видит все аккаунты в БД с `is_active=true`
   - Команды: start / stop / delete
   - Один автоперезапуск при падении, потом — `crashed` в Redis и `system.error`
   - Endpoint'ы `/workers`, `/workers/{id}/start`, `/workers/{id}/stop`, `/workers/stream` (SSE)
   - Синхронное ожидание завершения текущей задачи при stop

4. `modules/worker/worker.py` — жизненный цикл воркера
   - Отдельный asyncio-таск на каждый аккаунт
   - На старте: запускает враппер, подписывается на новые сообщения, обновляет статус в Redis
   - На остановке: graceful shutdown, статус в Redis

**Что нужно для теста:**
- Реальный Telegram-аккаунт с доступом к номеру (приём СМС)
- Пара SOCKS5 прокси (или поднять локальный тестовый через 3proxy в отдельном контейнере)
- 2FA на тестовом аккаунте лучше выключить (отдельно потом протестим 2FA ветку)

**Оценка времени:** 4-6 часов фокусной работы. В один присест.

---

## Формат старта Этапа 3

Открыть **новый чат** (этот уже перегружен историей, снижает качество длинных задач), прикрепить документацию из `docs/` (минимум: `architecture.md`, `api.md`, `event_bus.md`, `database_schema.md`, `auth.md`, `worker_manager.md`, `wrapper.md`), и написать что-то вроде:

> Продолжаем проект `finish-outrich`. Этапы 1 и 2 закрыты (инфра + шина событий), последний коммит `a0aedca` на main. Репо https://github.com/Aqua7MarcusAurelius/finish-outrich. Поехали к Этапу 3 — Telethon-враппер, модуль авторизации, менеджер воркеров. Прокси: [готовы / нужен тестовый]. Telegram-аккаунт: [есть / нет].

---

*Сгенерировано в финале сессии 21.04.2026.*
