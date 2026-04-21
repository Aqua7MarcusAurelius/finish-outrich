"""
Шина событий.

Живёт в Redis Streams — быстрая доставка живым подписчикам (SSE).
Параллельно фоновая задача (archive_writer_loop) читает поток через
consumer group и пишет события в PostgreSQL (таблица events_archive)
для фильтрации, экспорта и долгого хранения.

Поток событий:

    модуль вызывает publish(...)
        │
        ▼
    XADD в Redis Stream "events:stream" (maxlen ~10000)
        │
        ├──▶ archive_writer_loop (consumer group "archive-writer")
        │       └── INSERT в events_archive
        │
        └──▶ SSE-подписчики /events/stream
                └── XREAD по Last-Event-ID
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from core.redis import get_client

log = logging.getLogger(__name__)

STREAM_KEY = "events:stream"
ARCHIVE_GROUP = "archive-writer"
ARCHIVE_CONSUMER = "archive-writer-1"

# Ограничение длины потока — последние ~10k событий остаются в Redis.
# Архив живёт в Postgres, так что терять хвост в Redis некритично.
STREAM_MAXLEN = 10_000


# ─────────────────────────────────────────────────────────────────────
# Публикация
# ─────────────────────────────────────────────────────────────────────

def new_event_id() -> str:
    """32-символьный hex — UUID4 без дефисов."""
    return uuid.uuid4().hex


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def publish(
    *,
    module: str,
    type: str,
    status: str = "success",
    account_id: int | None = None,
    parent_id: str | None = None,
    data: dict[str, Any] | None = None,
    event_id: str | None = None,
    time: datetime | None = None,
) -> dict[str, Any]:
    """
    Опубликовать событие на шину.

    Возвращает словарь события (включая сгенерированный id).
    parent_id используется для сцепки событий в цепочку.
    """
    event = {
        "id": event_id or new_event_id(),
        "parent_id": parent_id,
        "time": (time or now_utc()).isoformat(),
        "account_id": account_id,
        "module": module,
        "type": type,
        "status": status,
        "data": data or {},
    }

    payload = json.dumps(event, ensure_ascii=False, default=str)

    client = get_client()
    await client.xadd(
        STREAM_KEY,
        {"event": payload},
        maxlen=STREAM_MAXLEN,
        approximate=True,
    )
    return event


# ─────────────────────────────────────────────────────────────────────
# Чтение — для архивного писателя (через consumer group)
# ─────────────────────────────────────────────────────────────────────

async def ensure_consumer_group() -> None:
    """Создать consumer group если её ещё нет. Идемпотентно."""
    client = get_client()
    try:
        await client.xgroup_create(STREAM_KEY, ARCHIVE_GROUP, id="0", mkstream=True)
        log.info("Created consumer group %s on %s", ARCHIVE_GROUP, STREAM_KEY)
    except Exception as e:
        # BUSYGROUP — группа уже существует, это норма
        if "BUSYGROUP" not in str(e):
            raise


def _decode_event(fields: dict) -> dict | None:
    """Распаковать JSON из полей Redis-сообщения."""
    raw = fields.get(b"event") or fields.get("event")
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.exception("Failed to decode event payload")
        return None


async def _read_for_archive(count: int = 100, block_ms: int = 5000) -> list[tuple[str, dict]]:
    """
    Прочитать батч новых событий для архивного писателя.
    Возвращает [(stream_id, event_dict), ...].
    """
    client = get_client()
    result = await client.xreadgroup(
        ARCHIVE_GROUP,
        ARCHIVE_CONSUMER,
        {STREAM_KEY: ">"},
        count=count,
        block=block_ms,
    )
    out = []
    if not result:
        return out
    for _stream_key, messages in result:
        for stream_id, fields in messages:
            event = _decode_event(fields)
            if event is None:
                # всё равно подтвердим, чтобы не ходить по нему бесконечно
                sid = stream_id.decode() if isinstance(stream_id, bytes) else stream_id
                await client.xack(STREAM_KEY, ARCHIVE_GROUP, sid)
                continue
            sid = stream_id.decode() if isinstance(stream_id, bytes) else stream_id
            out.append((sid, event))
    return out


async def _ack_archive(stream_ids: list[str]) -> None:
    if not stream_ids:
        return
    client = get_client()
    await client.xack(STREAM_KEY, ARCHIVE_GROUP, *stream_ids)


# ─────────────────────────────────────────────────────────────────────
# Чтение — для живых SSE-подписчиков (без consumer group)
# ─────────────────────────────────────────────────────────────────────

async def read_live(
    last_id: str = "$",
    count: int = 100,
    block_ms: int = 1000,
) -> list[tuple[str, dict]]:
    """
    Прочитать новые события для SSE-подписчика.
    last_id='$'  — только события после подключения
    last_id='0'  — с начала потока (не используем, хвост ограничен)
    last_id=X    — после события X (для Last-Event-ID)
    """
    client = get_client()
    result = await client.xread({STREAM_KEY: last_id}, count=count, block=block_ms)
    out = []
    if not result:
        return out
    for _stream_key, messages in result:
        for stream_id, fields in messages:
            event = _decode_event(fields)
            if event is None:
                continue
            sid = stream_id.decode() if isinstance(stream_id, bytes) else stream_id
            out.append((sid, event))
    return out


# ─────────────────────────────────────────────────────────────────────
# Архивный писатель — фоновая задача
# ─────────────────────────────────────────────────────────────────────

async def archive_writer_loop() -> None:
    """
    Бесконечный цикл: читает события из Redis Stream через consumer group
    и INSERT'ит в events_archive. Запускается как фоновая задача в lifespan.
    """
    # Импорт здесь чтобы избежать циклических импортов
    from core import db

    await ensure_consumer_group()
    log.info("archive_writer_loop started")

    while True:
        try:
            batch = await _read_for_archive(count=50, block_ms=5000)
            if not batch:
                continue

            pool = db.get_pool()
            ack_ids: list[str] = []

            async with pool.acquire() as conn:
                for stream_id, event in batch:
                    try:
                        # Время может прийти в разных форматах — нормализуем
                        time_val = event.get("time")
                        if isinstance(time_val, str):
                            time_dt = datetime.fromisoformat(time_val.replace("Z", "+00:00"))
                        else:
                            time_dt = now_utc()

                        await conn.execute(
                            """
                            INSERT INTO events_archive
                                (id, parent_id, time, account_id, module, type, status, data)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                            ON CONFLICT (id) DO NOTHING
                            """,
                            event["id"],
                            event.get("parent_id"),
                            time_dt,
                            event.get("account_id"),
                            event.get("module", "?"),
                            event.get("type", "?"),
                            event.get("status", "success"),
                            json.dumps(event.get("data") or {}, ensure_ascii=False, default=str),
                        )
                        ack_ids.append(stream_id)
                    except Exception:
                        log.exception("Failed to archive event %s", event.get("id"))
                        # Не ack'аем — попробуем в следующий проход

            if ack_ids:
                await _ack_archive(ack_ids)

        except asyncio.CancelledError:
            log.info("archive_writer_loop cancelled")
            raise
        except Exception:
            log.exception("archive_writer_loop error")
            await asyncio.sleep(1)
