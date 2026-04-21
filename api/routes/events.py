"""
Endpoints для работы с событиями шины.

    GET /events          — архив с фильтрами и курсорной пагинацией
    GET /events/{id}     — одно событие
    GET /events/stream   — SSE живой поток

К каждому событию при отдаче добавляется вычисляемое поле `message`
(см. core/event_messages.py).
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from api.sse import sse_format, sse_heartbeat
from core import bus, db
from core.event_messages import format_message

router = APIRouter(tags=["events"])


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _row_to_event(row: Any) -> dict[str, Any]:
    """Превратить строку из events_archive в словарь события."""
    data = row["data"]
    if isinstance(data, str):
        data = json.loads(data)
    return {
        "id": row["id"],
        "parent_id": row["parent_id"],
        "time": row["time"].isoformat(),
        "account_id": row["account_id"],
        "module": row["module"],
        "type": row["type"],
        "status": row["status"],
        "data": data or {},
    }


def _enrich(event: dict[str, Any]) -> dict[str, Any]:
    """Добавить вычисляемое поле `message`."""
    out = dict(event)
    out["message"] = format_message(event)
    return out


def _encode_cursor(time: datetime, event_id: str) -> str:
    payload = json.dumps({"t": time.isoformat(), "i": event_id})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return (
            datetime.fromisoformat(payload["t"].replace("Z", "+00:00")),
            payload["i"],
        )
    except Exception:
        raise HTTPException(400, {"error": {"code": "INVALID_CURSOR"}})


# ─────────────────────────────────────────────────────────────────────
# Архив
# ─────────────────────────────────────────────────────────────────────

@router.get("/events")
async def list_events(
    account_id: Optional[int] = None,
    module: Optional[str] = None,
    type: Optional[str] = None,
    status: Optional[str] = None,
    parent_id: Optional[str] = None,
    root_id: Optional[str] = Query(
        None,
        description="Вся цепочка от корня рекурсивно — для клика по ID в логе.",
    ),
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = None,
    limit: int = Query(100, ge=1, le=500),
    cursor: Optional[str] = None,
):
    """Архив событий с фильтрами и курсорной пагинацией."""
    pool = db.get_pool()

    # root_id — отдельный сценарий: вернуть всю цепочку от корня.
    # Другие фильтры и пагинация в этом режиме не применяются —
    # цепочка обычно небольшая, UI показывает её целиком.
    if root_id:
        sql = """
            WITH RECURSIVE chain AS (
                SELECT * FROM events_archive WHERE id = $1
                UNION ALL
                SELECT e.* FROM events_archive e
                INNER JOIN chain c ON e.parent_id = c.id
            )
            SELECT * FROM chain
            ORDER BY time ASC, id ASC
            LIMIT $2
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, root_id, limit)
        return {
            "events": [_enrich(_row_to_event(r)) for r in rows],
            "next_cursor": None,
        }

    # Обычный режим — фильтры + курсорная пагинация
    where: list[str] = []
    params: list[Any] = []

    def _add(clause: str, value: Any) -> None:
        params.append(value)
        where.append(clause.replace("?", f"${len(params)}"))

    if account_id is not None:
        _add("account_id = ?", account_id)
    if module:
        _add("module = ?", module)
    if type:
        _add("type = ?", type)
    if status:
        _add("status = ?", status)
    if parent_id:
        _add("parent_id = ?", parent_id)
    if from_:
        _add("time >= ?", from_)
    if to:
        _add("time <= ?", to)

    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        params.append(cursor_time)
        params.append(cursor_id)
        where.append(f"(time, id) < (${len(params) - 1}, ${len(params)})")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT * FROM events_archive
        {where_sql}
        ORDER BY time DESC, id DESC
        LIMIT {limit + 1}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    has_more = len(rows) > limit
    rows = rows[:limit]

    events = [_enrich(_row_to_event(r)) for r in rows]

    next_cursor: Optional[str] = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last["time"], last["id"])

    return {"events": events, "next_cursor": next_cursor}


# ─────────────────────────────────────────────────────────────────────
# Живой SSE-поток
#
# ВАЖНО: этот роут объявлен ДО /events/{event_id} — иначе FastAPI
# заматчит "stream" как event_id и уведёт запрос не туда.
# ─────────────────────────────────────────────────────────────────────

@router.get("/events/stream")
async def stream_events(request: Request):
    """
    SSE-поток всех событий шины.

    При переподключении браузер шлёт заголовок Last-Event-ID
    (это будет Redis-Stream-ID последнего виденного события) —
    мы стартуем XREAD с него и досылаем пропущенное.
    """
    # Если Last-Event-ID нет — '$' = только новые события после подключения
    last_event_id = request.headers.get("Last-Event-ID") or "$"

    async def generator():
        # Первый heartbeat сразу — пробивает буферы прокси и открывает соединение
        yield sse_heartbeat()

        last_id = last_event_id
        idle_counter = 0

        while True:
            if await request.is_disconnected():
                return

            try:
                # block=1000ms — чтобы каждую секунду проверять disconnect
                batch = await bus.read_live(last_id=last_id, count=50, block_ms=1000)
            except Exception:
                # Redis мигнул — шлём heartbeat и пробуем снова
                yield sse_heartbeat()
                await asyncio.sleep(1)
                continue

            if not batch:
                idle_counter += 1
                # Heartbeat раз в ~30 сек тишины
                if idle_counter >= 30:
                    yield sse_heartbeat()
                    idle_counter = 0
                continue

            idle_counter = 0
            for stream_id, event in batch:
                last_id = stream_id
                enriched = _enrich(event)
                yield sse_format(
                    event="event",
                    data=enriched,
                    id=stream_id,
                )

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # защита от буферизации в nginx/прокси
        },
    )


# ─────────────────────────────────────────────────────────────────────
# Одно событие по id — объявлен ПОСЛЕ /events/stream, см. комментарий выше
# ─────────────────────────────────────────────────────────────────────

@router.get("/events/{event_id}")
async def get_event(event_id: str):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM events_archive WHERE id = $1",
            event_id,
        )
    if not row:
        raise HTTPException(404, {"error": {"code": "EVENT_NOT_FOUND"}})
    return _enrich(_row_to_event(row))
