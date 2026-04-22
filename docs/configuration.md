# Конфигурация проекта

> Два уровня конфигурации:
> - `.env` — секреты и адреса сервисов. Нужны для старта системы.
> - База данных (таблица `settings`) — настройки поведения модулей. Можно менять без перезапуска.

---

## .env.example

Этот файл коммитится в GitHub — без значений, только ключи. Разработчик копирует его в `.env` и заполняет своими данными.

```env
# ───────────────────────────────
# Приложение
# ───────────────────────────────
APP_ENV=development           # development / production
APP_PORT=8000
API_TOKEN=                    # токен для авторизации запросов к API
DOCS_PUBLIC=true              # открыт ли /docs наружу (выключить в production)

# ───────────────────────────────
# CORS
# ───────────────────────────────
CORS_ORIGINS=http://localhost:3000

# ───────────────────────────────
# PostgreSQL
# ───────────────────────────────
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=tgframework
POSTGRES_USER=
POSTGRES_PASSWORD=

# ───────────────────────────────
# Redis
# ───────────────────────────────
REDIS_HOST=redis
REDIS_PORT=6379

# ───────────────────────────────
# MinIO
# ───────────────────────────────
MINIO_HOST=minio
MINIO_PORT=9000
MINIO_ROOT_USER=
MINIO_ROOT_PASSWORD=
MINIO_BUCKET=tgframework

# ───────────────────────────────
# OpenRouter
# ───────────────────────────────
OPENROUTER_API_KEY=
OPENROUTER_MODEL_TRANSCRIPTION=openai/whisper
OPENROUTER_MODEL_DESCRIPTION=openai/gpt-4o
OPENROUTER_MODEL_AUTOCHAT=anthropic/claude-opus-4-7     # уточнить точное имя в каталоге OpenRouter
```

---

## Настройки в базе данных — таблица `settings`

Можно менять без перезапуска системы. Структура таблицы описана в `database_schema.md`. Дефолтные значения создаются первой миграцией Alembic.

### Настройки и их значения по умолчанию

| Ключ | Значение по умолчанию | Описание |
|---|---|---|
| `cleaner.interval_hours` | `1` | Как часто запускается чистильщик (в часах) |
| `cleaner.batch_size` | `50` | Сколько файлов удаляет за один прогон |
| `cleaner.file_ttl_days` | `3` | Сколько дней хранятся файлы в MinIO |
| `transcription.retries` | `1` | Сколько повторных попыток при ошибке OpenRouter |
| `description.retries` | `1` | Сколько повторных попыток при ошибке OpenRouter |
| `description.frames_count` | `5` | Сколько кадров нарезает FFmpeg из видео / GIF / кружков |
| `history_sync.chunk_size` | `100` | Сколько сообщений запрашивает нагон за один раз |
| `autochat.enter_delay_short_sec` | `15` | Задержка входа в чат при возрасте последнего сообщения 0–5 мин |
| `autochat.enter_delay_mid_sec` | `60` | То же для 5–10 мин |
| `autochat.enter_delay_long_sec` | `120` | То же для ≥10 мин |
| `autochat.idle_leave_sec` | `180` | Через сколько тишины в чате уходим в `InChat=0` (3 мин) |
| `autochat.reply_timer_sec` | `30` | Базовый reply-таймер перед запросом в LLM |
| `autochat.openrouter_retries` | `2` | Ретраи при ошибке OpenRouter в автодиалогах |
| `autochat.typing_ms_per_char` | `40` | Имитация печати — миллисекунд на символ |

---

## .gitignore

```
# Секреты
.env

# Python
__pycache__/
*.pyc
*.pyo
.venv/

# Локальные данные
*.log

# IDE
.idea/
.vscode/
```

---

## Принципы которые мы зафиксировали

1. `.env` — только секреты и адреса сервисов. То без чего система не стартует
2. `.env.example` коммитим в GitHub — `.env` никогда
3. Настройки поведения модулей — в таблице `settings` в базе. Можно менять без перезапуска
4. Значения по умолчанию для всех настроек заданы в первой миграции — система работает из коробки
5. В production `DOCS_PUBLIC=false` — закрывает `/docs` и `/openapi.json` от внешнего мира
