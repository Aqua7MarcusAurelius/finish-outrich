# Аудит проекта Telegram Automation Framework

> Дата: 2026-04-22
> Версия проекта: 0.6.0 (после Этапа 6)
> Статус: локальный запуск работает, перед внешним деплоем нужна доработка.

---

## TL;DR

- **Локально работает** — все критические находки либо не проявляются локально, либо не проявляются до определённого триггера (большой файл, кривое событие, открытый порт).
- **Перед продом** обязательно починить 2 пункта: `--reload` в Dockerfile и poison-message в consumer loops.
- **Перед выходом наружу** подключить авторизацию (`API_TOKEN` уже лежит в конфиге, но не используется).
- Остальное — планомерная полировка.

---

## 🔴 Блокеры: починить до первого прод-деплоя

### 1. `--reload` в production Dockerfile

**Где:** [Dockerfile:23](../Dockerfile#L23)

```dockerfile
CMD ["sh", "-c", "alembic upgrade head && uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload"]
```

**Что это:** флаг uvicorn, который следит за `.py` файлами и перезапускает сервер при любом изменении. Dev-фича для горячей перезагрузки.

**Почему попал:** в [docker-compose.yml:17-18](../docker-compose.yml#L17-L18) код монтируется как volume `./:/app` — правки на хосте сразу видны в контейнере. Вместе с `--reload` получается hot-reload. Комментарий в Dockerfile (L22) сам признаёт: *«для prod убрать»*, но не убрано.

**Почему плохо на проде:**
- Рвёт открытые SSE-соединения дашборда.
- Обрывает Telethon-клиентов — сессии с Telegram теряются, воркеры надо восстанавливать.
- Consumer loop'ы Redis Streams теряют прогресс.
- Watcher файлов жрёт CPU зазря.

**Как исправить (просто):**

```dockerfile
CMD ["sh", "-c", "alembic upgrade head && uvicorn api.main:app --host 0.0.0.0 --port 8000"]
```

Если для dev нужен hot-reload — запускать локально без Docker (`uvicorn api.main:app --reload`) или сделать `docker-compose.dev.yml` с override команды.

---

### 2. Poison message в consumer loop'ах

**Где:**
- [modules/history/service.py:98-112](../modules/history/service.py#L98-L112)
- аналогично в `modules/transcription/service.py`
- аналогично в `modules/media_description/service.py`

**Как работает шина:**
- Сервис читает событие из Redis Streams через consumer group.
- Обрабатывает → вызывает `XACK` → событие помечено как обработанное.
- Если `XACK` не вызван — событие остаётся в pending-list, при рестарте или запросе `XREADGROUP id=0` вернётся снова.

**Проблема:** если событие **всегда** падает при обработке (кривой JSON, отсутствует обязательное поле, баг в коде), оно копится в pending и никогда не ack'ается. Сейчас код просто логирует исключение и идёт дальше:

```python
try:
    await self._handle(event)
    ack_ids.append(stream_id)
except Exception:
    log.exception("history: failed to handle event %s", event.get("id"))
    # Не ack'аем — переобработка в следующем прогоне.
```

**Последствия:**
1. Pending-list растёт бесконечно.
2. При рестарте, если решишь перечитать pending (`XREADGROUP id=0`) — loop зацикливается на ядовитом сообщении.
3. Баг в обработчике маскируется — внешне всё работает, а события одного типа тихо падают. Заметишь через месяц, когда полезешь смотреть почему в истории дыры.

**Как исправить:** счётчик попыток на событие + dead-letter. Один раз в `core/bus.py` — защищает все consumer'ы.

Набросок:

```python
# В core/bus.py
MAX_RETRIES = 5

async def handle_with_retries(stream_id: str, event: dict, handler):
    retry_key = f"bus:retries:{stream_id}"
    try:
        await handler(event)
        await redis.delete(retry_key)
        return True  # можно ack'ать
    except Exception as e:
        retries = await redis.incr(retry_key)
        await redis.expire(retry_key, 86400)
        if retries >= MAX_RETRIES:
            log.error("poison message %s after %d retries: %s", stream_id, retries, e)
            await publish_system_error("poison_message", stream_id=stream_id, error=str(e))
            await redis.delete(retry_key)
            return True  # ack'аем, чтобы не зависало
        return False  # не ack'аем, попробуем ещё
```

Consumer'ы заменяют свой inline try/except на этот helper.

---

## 🟠 Важно, но не блокер (починить после первого прода)

### 3. API без авторизации

**Где:** [core/config.py:23](../core/config.py#L23) — `API_TOKEN: str = ""` определён, но нигде не используется.

Проверил: ни одного `Depends`, `HTTPBearer`, `verify_token` в коде нет. README прямо говорит: *«на этом этапе не проверяется, но пусть будет»*.

**Когда становится проблемой:** как только откроешь порт 8000 наружу или на общую сеть. Локально — не проблема.

**Как исправить:**

```python
# api/deps.py (новый файл)
from fastapi import Header, HTTPException
import secrets
from core.config import settings

async def verify_token(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization[7:]
    if not secrets.compare_digest(token, settings.API_TOKEN):
        raise HTTPException(401, "invalid token")
```

Подключить в роутерах как `dependencies=[Depends(verify_token)]`. Исключение — `/system/health` (healthcheck нужен без токена) и `/auth/*` (запуск авторизации).

---

### 4. OpenRouter без retry и singleton client

**Где:** [core/openrouter.py:75](../core/openrouter.py#L75)

```python
async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
    resp = await client.post(OPENROUTER_URL, json=payload, headers=_headers())
```

**Проблемы:**
- Новый `AsyncClient` на каждый запрос → новый TLS handshake, нет keep-alive.
- При `429 Too Many Requests` нет парсинга `Retry-After`, нет backoff.
- При `5xx` любой вызов сразу падает — consumer повторяет в tight loop.

**Как исправить:**
- Глобальный клиент в `lifespan`:
```python
# api/main.py
app.state.http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
# shutdown:
await app.state.http_client.aclose()
```
- Exponential backoff на 429/503 внутри `_post`:
```python
for attempt in range(3):
    resp = await client.post(...)
    if resp.status_code in (429, 503):
        wait = int(resp.headers.get("Retry-After", 2 ** attempt))
        await asyncio.sleep(wait)
        continue
    break
```

---

### 5. Конфиги читаются из БД на каждое событие

**Где:** `modules/transcription/service.py`, `modules/media_description/service.py` — `_get_int_setting` делает SELECT для каждого медиа.

**Последствия:** при батче 50 сообщений — 50+ одинаковых SELECT'ов.

**Как исправить:** TTL-кэш (30-60 сек) в `core/config.py`:

```python
from cachetools import TTLCache
_settings_cache: TTLCache = TTLCache(maxsize=32, ttl=60)

async def get_setting_int(key: str, default: int) -> int:
    if key in _settings_cache:
        return _settings_cache[key]
    value = await db.fetchval("SELECT value::int FROM settings WHERE key=$1", key) or default
    _settings_cache[key] = value
    return value
```

---

### 6. Shutdown воркеров последовательный

**Где:** [modules/worker_manager/service.py:177-182](../modules/worker_manager/service.py#L177-L182)

```python
await asyncio.wait_for(slot.task, timeout=30)
```

При N воркерах — N × 30 сек в худшем случае.

**Как исправить:** параллельная остановка через `asyncio.gather(..., return_exceptions=True)`.

---

### 7. FloodWait без экспоненциального backoff в нагоне

**Где:** [modules/history_sync/service.py](../modules/history_sync/service.py)

Ловит FloodWait, спит `wait` секунд, продолжает. Следующий батч может опять получить FloodWait — часы на один диалог.

**Как исправить:** на каждый повторный FloodWait увеличивать chunk в 2 раза и добавлять jitter между батчами.

---

### 8. Потеря медиа при ошибке скачивания

**Где:** [modules/worker/worker.py](../modules/worker/worker.py), [modules/history_sync/service.py](../modules/history_sync/service.py)

Если `download_media_bytes` или `minio.put_object` падает — `message.received` публикуется с пустым списком медиа. История получает «текстовое» сообщение, хотя в Telegram была картинка.

**Как исправить:** публиковать событие с `media: [{id, type, status: "failed", reason: ...}]` — тогда потом можно перескачать.

---

## 🟡 Полировка (когда будут силы)

### 9. Root пользователь в контейнере
[Dockerfile](../Dockerfile) — нет `USER`. При RCE через обработку медиа компрометируется весь контейнер.
**Чинить:** `RUN useradd -m app && chown -R app /app && USER app`.

### 10. MinIO bucket без private policy
Если случайно открыть порт 9000 наружу — публичный листинг всех медиа.
**Чинить:** `set_bucket_policy(deny-all-anonymous)` + не выставлять порт 9000 в compose.

### 11. Валидация `change_me` в .env
Можно случайно запустить прод с паролем `change_me`. Валидация при старте в `core/config.py`.

### 12. Маскировка секретов в логах
[core/openrouter.py:81](../core/openrouter.py#L81) — `resp.text[:500]` идёт в исключение и логи. Если в ответе окажется ключ — утечка. Маскировать перед логированием.

### 13. `--reload` уже здесь ↑, но добавить: разделить Dockerfile на dev/prod
Multi-stage build или отдельный `Dockerfile.dev`, либо `docker-compose.dev.yml` с override команды.

### 14. MAXLEN на архивный писатель + мониторинг отставания
[core/bus.py](../core/bus.py) — `STREAM_MAXLEN=10_000` с `approximate=True`. При всплеске события выдавливаются до попадания в архив.
**Чинить:** алерт при `XLEN stream > 0.8 * MAXLEN`, метрика отставания `XPENDING`.

### 15. Event schema без версионирования
Нет `schema_version` в events. При эволюции полей старый архив перестанет парситься.

### 16. Нет FTS индекса на `messages.text`
`GIN(to_tsvector('russian', text))` — если появится поиск по сообщениям.

### 17. Downgrade миграций сносит всё `DROP TABLE IF EXISTS`
[alembic/versions/0001_initial.py](../alembic/versions/0001_initial.py) — случайный `alembic downgrade base` в проде = потеря всех данных. Блокировать downgrade в env.py по `APP_ENV=production`.

### 18. Cleaner ломается на дробных TTL
[modules/history/cleaner.py:86](../modules/history/cleaner.py#L86) — `int(ttl_days * 86400)` для `ttl_days=0.01` → 0 сек. Использовать `timedelta(days=ttl_days)` (принимает float).

### 19. TGS-стикер проверяется только по MIME
[modules/media_description/service.py:244](../modules/media_description/service.py#L244) — если wrapper не передал mime, TGS уйдёт в GPT-4o и упадёт HTTP 400.

### 20. Дублирование retry-логики между transcription и media_description
Вынести `with_retries(coro, n=...)` в `core/openrouter.py`.

### 21. Health-check Telethon клиента
Периодический `client.get_me()` в воркере — обнаружить «умершие» сессии раньше.

### 22. requirements.txt без lock-файла
Версии пришпилены `==`, но транзитивные зависимости могут плавать. `pip-tools` + `requirements.lock`.

---

## Известные долги из отчётов этапов

Из [docs/fixes/transcription_issues.md](./fixes/transcription_issues.md) и отчётов:

- OpenRouter retry без backoff — busy-loop на одном событии блокирует батч (пересекается с п. 4).
- Неограниченные retries: если admin выставит `retries=100`, сделается 101 попытка подряд. Нужен cap на 5.
- Скрытый retry на пустой результат в транскрипции маскирует ошибки. Нужен статус `done_empty` или явный флаг.
- Статус `pending` на транскрипции не переходит в `in_progress` — UI не показывает прогресс.
- `audio_format` параметр — мёртвый код, всегда `"wav"`.

---

## Приоритеты

| # | Задача | Критичность | Оценка |
|---|---|---|---|
| 1 | Убрать `--reload` из Dockerfile | блокер прода | 5 минут |
| 2 | Dead-letter для consumer loops | блокер стабильности | полдня |
| 3 | Подключить `API_TOKEN` | перед внешним деплоем | 1-2 часа |
| 4 | Retry/backoff + singleton httpx | важно | 2-3 часа |
| 5 | Кэш settings с TTL | важно | 1 час |
| 6 | Параллельный shutdown воркеров | важно | 1 час |
| 7 | Root → non-root в Docker | полировка | 30 минут |
| 8 | Остальная полировка (п. 10-22) | фоном | по мере нужды |

---

## Что с архитектурой в целом

Проект собран чисто:
- Модули не знают друг друга — только шина.
- API не лезет в модули напрямую — только через Redis и worker manager.
- SQL руками через asyncpg — читаемо, без ORM-магии.
- Каждая таблица имеет одного владельца — модуль.
- События — единый лог системы, никаких параллельных логгеров.

Это хороший фундамент. Слабые места — на периметре (безопасность, обработка ошибок внешних сервисов, streaming медиа). Всё чинится инкрементально без переписывания.
