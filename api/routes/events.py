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
import csv
import io
import json
from datetime import datetime, timedelta, timezone
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


def _build_filter_sql(
    *,
    account_id: Optional[int],
    module: Optional[str],
    type: Optional[str],
    status: Optional[str],
    from_: Optional[datetime],
    to: Optional[datetime],
    dialog_id: Optional[int] = None,
) -> tuple[str, list[Any]]:
    """Собрать WHERE-клоз и параметры. type с суффиксом `*` → LIKE-префикс.
    dialog_id фильтрует по JSONB-полю `data->>'dialog_id'` — удобно для
    мини-лога конкретного диалога в UI."""
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
        if type.endswith("*"):
            _add("type LIKE ?", type[:-1] + "%")
        else:
            _add("type = ?", type)
    if status:
        _add("status = ?", status)
    if dialog_id is not None:
        # JSONB оператор ->> возвращает text, кастуем в bigint и сравниваем.
        # На маленьких объёмах хватает sequential-scan; для горячего пути
        # можно будет добавить GIN или функциональный индекс отдельно.
        _add("(data->>'dialog_id')::bigint = ?", dialog_id)
    if from_:
        _add("time >= ?", from_)
    if to:
        _add("time <= ?", to)

    return (" AND ".join(where), params)


def _match_filters_memory(
    event: dict[str, Any],
    *,
    account_id: Optional[int],
    module: Optional[str],
    type: Optional[str],
    status: Optional[str],
    dialog_id: Optional[int] = None,
) -> bool:
    """Серверная фильтрация SSE-потока — повторяем семантику _build_filter_sql."""
    if account_id is not None and event.get("account_id") != account_id:
        return False
    if module and event.get("module") != module:
        return False
    if type:
        ev_type = event.get("type") or ""
        if type.endswith("*"):
            if not ev_type.startswith(type[:-1]):
                return False
        elif ev_type != type:
            return False
    if status and event.get("status") != status:
        return False
    if dialog_id is not None:
        data = event.get("data") or {}
        raw = data.get("dialog_id")
        if raw is None:
            return False
        try:
            if int(raw) != dialog_id:
                return False
        except (TypeError, ValueError):
            return False
    return True


# ─────────────────────────────────────────────────────────────────────
# Архив
# ─────────────────────────────────────────────────────────────────────

@router.get("/events")
async def list_events(
    account_id: Optional[int] = Query(None, alias="account"),
    module: Optional[str] = None,
    type: Optional[str] = None,
    status: Optional[str] = None,
    dialog_id: Optional[int] = None,
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
    where_expr, params = _build_filter_sql(
        account_id=account_id, module=module, type=type, status=status,
        from_=from_, to=to, dialog_id=dialog_id,
    )
    where_parts = [where_expr] if where_expr else []
    if parent_id:
        params.append(parent_id)
        where_parts.append(f"parent_id = ${len(params)}")

    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        params.append(cursor_time)
        params.append(cursor_id)
        where_parts.append(f"(time, id) < (${len(params) - 1}, ${len(params)})")

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

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

# ─────────────────────────────────────────────────────────────────────
# GET /events/stats — 5 агрегатов для карточек метрик
# ─────────────────────────────────────────────────────────────────────

@router.get("/events/stats")
async def events_stats(
    account_id: Optional[int] = Query(None, alias="account"),
    module: Optional[str] = None,
    type: Optional[str] = None,
    status: Optional[str] = None,
    dialog_id: Optional[int] = None,
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = None,
):
    """5 чисел: total / success / error / in_progress / events_per_sec."""
    now = datetime.now(timezone.utc)
    if not from_:
        from_ = now - timedelta(hours=1)
    if not to:
        to = now

    where_expr, params = _build_filter_sql(
        account_id=account_id, module=module, type=type, status=status,
        from_=from_, to=to, dialog_id=dialog_id,
    )
    where_sql = ("WHERE " + where_expr) if where_expr else ""

    # Скользящее окно 60 сек для events/sec считаем отдельно, без фильтра по
    # времени — иначе в маленьких окнах (1 сек) оно шумит как сумасшедшее.
    eps_where, eps_params = _build_filter_sql(
        account_id=account_id, module=module, type=type, status=status,
        from_=now - timedelta(seconds=60), to=now, dialog_id=dialog_id,
    )
    eps_sql = ("WHERE " + eps_where) if eps_where else ""

    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT
                COUNT(*)::int                                       AS total,
                COUNT(*) FILTER (WHERE status = 'success')::int     AS success,
                COUNT(*) FILTER (WHERE status = 'error')::int       AS error,
                COUNT(*) FILTER (WHERE status = 'in_progress')::int AS in_progress
            FROM events_archive
            {where_sql}
            """,
            *params,
        )
        eps_row = await conn.fetchrow(
            f"SELECT COUNT(*)::int AS c FROM events_archive {eps_sql}",
            *eps_params,
        )

    return {
        "total":          row["total"]       if row else 0,
        "success":        row["success"]     if row else 0,
        "error":          row["error"]       if row else 0,
        "in_progress":    row["in_progress"] if row else 0,
        "events_per_sec": (eps_row["c"] / 60.0) if eps_row else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────
# GET /events/export — стриминговый CSV / JSON
# ─────────────────────────────────────────────────────────────────────

_EXPORT_BATCH = 1000  # строк за один SELECT
_EXPORT_LIMIT = 100_000  # жёсткий потолок — лог может быть миллионами строк


@router.get("/events/export")
async def events_export(
    format: str = Query("csv", pattern="^(csv|json)$"),
    account_id: Optional[int] = Query(None, alias="account"),
    module: Optional[str] = None,
    type: Optional[str] = None,
    status: Optional[str] = None,
    dialog_id: Optional[int] = None,
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = None,
):
    """Экспорт событий с теми же фильтрами что у /events. Стрим — файл начинает
    качаться сразу, страница не блокируется. Потолок — _EXPORT_LIMIT строк."""
    where_expr, params = _build_filter_sql(
        account_id=account_id, module=module, type=type, status=status,
        from_=from_, to=to, dialog_id=dialog_id,
    )
    where_sql = ("WHERE " + where_expr) if where_expr else ""

    pool = db.get_pool()

    async def _fetch_batch(conn, last_time: Optional[datetime], last_id: Optional[str]):
        clauses = [where_expr] if where_expr else []
        batch_params = list(params)
        if last_time and last_id:
            batch_params.append(last_time)
            batch_params.append(last_id)
            clauses.append(f"(time, id) < (${len(batch_params) - 1}, ${len(batch_params)})")
        clause_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT * FROM events_archive
            {clause_sql}
            ORDER BY time DESC, id DESC
            LIMIT {_EXPORT_BATCH}
        """
        return await conn.fetch(sql, *batch_params)

    async def csv_generator():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "parent_id", "time", "account_id", "module", "type", "status", "data_json"])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        async with pool.acquire() as conn:
            last_time: Optional[datetime] = None
            last_id: Optional[str] = None
            total = 0
            while total < _EXPORT_LIMIT:
                rows = await _fetch_batch(conn, last_time, last_id)
                if not rows:
                    break
                for r in rows:
                    writer.writerow([
                        r["id"], r["parent_id"], r["time"].isoformat() if r["time"] else "",
                        r["account_id"], r["module"], r["type"], r["status"],
                        json.dumps(r["data"] if not isinstance(r["data"], str) else json.loads(r["data"]),
                                   ensure_ascii=False, separators=(",", ":")),
                    ])
                    total += 1
                    if total >= _EXPORT_LIMIT:
                        break
                last_time = rows[-1]["time"]
                last_id = rows[-1]["id"]
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)

    async def json_generator():
        yield "["
        first = True
        async with pool.acquire() as conn:
            last_time: Optional[datetime] = None
            last_id: Optional[str] = None
            total = 0
            while total < _EXPORT_LIMIT:
                rows = await _fetch_batch(conn, last_time, last_id)
                if not rows:
                    break
                for r in rows:
                    ev = _enrich(_row_to_event(r))
                    yield ("," if not first else "") + json.dumps(ev, ensure_ascii=False)
                    first = False
                    total += 1
                    if total >= _EXPORT_LIMIT:
                        break
                last_time = rows[-1]["time"]
                last_id = rows[-1]["id"]
        yield "]"

    if format == "csv":
        return StreamingResponse(
            csv_generator(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="events.csv"'},
        )
    return StreamingResponse(
        json_generator(),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="events.json"'},
    )


# ─────────────────────────────────────────────────────────────────────
# Живой SSE-поток — ПОСЛЕ всех /events/<that's-not-a-stream> но ДО /events/{id}
# ─────────────────────────────────────────────────────────────────────

@router.get("/events/stream")
async def stream_events(
    request: Request,
    account_id: Optional[int] = Query(None, alias="account"),
    module: Optional[str] = None,
    type: Optional[str] = None,
    status: Optional[str] = None,
    dialog_id: Optional[int] = None,
):
    """
    SSE-поток. Фильтрация — на сервере: в UI всегда прилетают только события
    под текущую выборку. Last-Event-ID обрабатывается для переподключения.
    """
    last_event_id = request.headers.get("Last-Event-ID") or "$"

    async def generator():
        yield sse_heartbeat()

        last_id = last_event_id
        idle_counter = 0

        while True:
            if await request.is_disconnected():
                return

            try:
                batch = await bus.read_live(last_id=last_id, count=50, block_ms=1000)
            except Exception:
                yield sse_heartbeat()
                await asyncio.sleep(1)
                continue

            if not batch:
                idle_counter += 1
                if idle_counter >= 30:
                    yield sse_heartbeat()
                    idle_counter = 0
                continue

            idle_counter = 0
            for stream_id, event in batch:
                last_id = stream_id
                if not _match_filters_memory(
                    event, account_id=account_id, module=module,
                    type=type, status=status, dialog_id=dialog_id,
                ):
                    continue
                enriched = _enrich(event)
                yield sse_format(event="event", data=enriched, id=stream_id)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─────────────────────────────────────────────────────────────────────
# GET /events/{id}/chain — цепочка parent_id + descendants
# Объявлен ДО /events/{event_id} чтобы не перехватывался плоским маршрутом.
# ─────────────────────────────────────────────────────────────────────

@router.get("/events/{event_id}/chain")
async def get_event_chain(event_id: str):
    """Предки по parent_id + само событие + прямые потомки.
    Depth capped на 50 уровней чтобы не уйти в бесконечность при битых данных."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        center = await conn.fetchrow(
            "SELECT * FROM events_archive WHERE id = $1", event_id,
        )
        if not center:
            raise HTTPException(404, {"error": {"code": "EVENT_NOT_FOUND"}})

        ancestors = await conn.fetch(
            """
            WITH RECURSIVE up AS (
                SELECT e.*, 0 AS depth FROM events_archive e WHERE e.id = $1
                UNION ALL
                SELECT p.*, u.depth + 1
                FROM events_archive p
                INNER JOIN up u ON p.id = u.parent_id
                WHERE u.depth < 50 AND u.parent_id IS NOT NULL
            )
            SELECT * FROM up WHERE id <> $1 ORDER BY depth DESC
            """,
            event_id,
        )

        descendants = await conn.fetch(
            """
            WITH RECURSIVE down AS (
                SELECT e.*, 0 AS depth FROM events_archive e WHERE e.id = $1
                UNION ALL
                SELECT c.*, d.depth + 1
                FROM events_archive c
                INNER JOIN down d ON c.parent_id = d.id
                WHERE d.depth < 50
            )
            SELECT * FROM down WHERE id <> $1 ORDER BY time ASC, id ASC
            """,
            event_id,
        )

    def _slim(r: Any) -> dict[str, Any]:
        return {"id": r["id"], "module": r["module"], "type": r["type"], "status": r["status"]}

    return {
        "ancestors":   [_slim(r) for r in ancestors],
        "event":       _enrich(_row_to_event(center)),
        "descendants": [_slim(r) for r in descendants],
    }


# ─────────────────────────────────────────────────────────────────────
# Одно событие по id — объявлен ПОСЛЕ всех более специфичных роутов
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
