"""
Endpoints модуля истории:

    GET   /accounts/{id}/dialogs    — список диалогов аккаунта
    GET   /dialogs/{id}             — карточка диалога + агрегаты
    GET   /dialogs/{id}/messages    — сообщения с курсорной пагинацией
    GET   /dialogs/{id}/stream      — SSE: message.saved / message.updated для диалога
    POST  /dialogs/{id}/read        — пометить весь диалог прочитанным (через враппер)
    GET   /messages/{id}            — одно сообщение
    POST  /accounts/{id}/messages   — отправить сообщение (синхронно ждём message.saved)

Читаем напрямую из БД (asyncpg) — см. принцип из docs/api.md.
Команды в Telegram — только через WorkerManager.get_wrapper.
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.sse import sse_format, sse_heartbeat
from core import bus, db
from core import minio as minio_mod
from core.events import EventType, Module, Status
from modules.worker.wrapper import SessionExpired, WrapperError

router = APIRouter(tags=["history"])


# ─────────────────────────────────────────────────────────────────────
# Сериализаторы (row → dict)
# ─────────────────────────────────────────────────────────────────────

def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _dialog_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "telegram_user_id": row["telegram_user_id"],
        "type": row["type"],
        "username": row["username"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "phone": row["phone"],
        "birthday": _iso(row["birthday"]),
        "bio": row["bio"],
        "is_contact": row["is_contact"],
        "contact_first_name": row["contact_first_name"],
        "contact_last_name": row["contact_last_name"],
        "is_bot": row["is_bot"],
        # Чисто визуальный статус оператора (UI-only). Допустимые значения
        # определены на фронте: talking/waiting/done/failed. None = «без статуса».
        "user_status": row["user_status"] if "user_status" in row.keys() else None,
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _media_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "message_id": row["message_id"],
        "type": row["type"],
        "file_name": row["file_name"],
        "telegram_file_id": row["telegram_file_id"],
        "storage_key": row["storage_key"],
        "mime_type": row["mime_type"],
        "file_size": row["file_size"],
        "duration": row["duration"],
        "width": row["width"],
        "height": row["height"],
        "transcription": row["transcription"],
        "transcription_status": row["transcription_status"],
        "description": row["description"],
        "description_status": row["description_status"],
        "downloaded_at": _iso(row["downloaded_at"]),
        "file_deleted_at": _iso(row["file_deleted_at"]),
    }


def _forward_from(row: Any) -> dict[str, Any] | None:
    # Фолбэк: если ни одного поля про пересылку не заполнено — пересылки не было
    if not (
        row["forward_from_user_id"]
        or row["forward_from_name"]
        or row["forward_from_chat_id"]
        or row["forward_date"]
    ):
        return None
    return {
        "user_id": row["forward_from_user_id"],
        "username": row["forward_from_username"],
        "name": row["forward_from_name"],
        "chat_id": row["forward_from_chat_id"],
        "date": _iso(row["forward_date"]),
    }


def _message_to_dict(
    row: Any,
    media: list[dict[str, Any]] | None = None,
    reactions: list[dict[str, Any]] | None = None,
    reply_to_tg: int | None = None,
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "dialog_id": row["dialog_id"],
        "telegram_message_id": row["telegram_message_id"],
        "is_outgoing": row["is_outgoing"],
        "type": row["type"],
        "date": _iso(row["date"]),
        "text": row["text"],
        "reply_to_message_id": row["reply_to_message_id"],
        "reply_to_telegram_message_id": reply_to_tg,
        "forward_from": _forward_from(row),
        "media_group_id": row["media_group_id"],
        "edited_at": _iso(row["edited_at"]),
        "deleted_at": _iso(row["deleted_at"]),
        "media": media or [],
        "reactions": reactions or [],
    }


# ─────────────────────────────────────────────────────────────────────
# Курсор для messages: (date, id)
# ─────────────────────────────────────────────────────────────────────

def _encode_msg_cursor(date: datetime, msg_id: int) -> str:
    payload = json.dumps({"d": date.isoformat(), "i": msg_id})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_msg_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return (
            datetime.fromisoformat(payload["d"].replace("Z", "+00:00")),
            int(payload["i"]),
        )
    except Exception:
        raise HTTPException(400, {"error": {"code": "INVALID_CURSOR"}})


# ─────────────────────────────────────────────────────────────────────
# Вспомогательный fetch медиа + reply-telegram-id для батча сообщений
# ─────────────────────────────────────────────────────────────────────

async def _fetch_media_by_messages(conn, message_ids: list[int]) -> dict[int, list[dict]]:
    if not message_ids:
        return {}
    rows = await conn.fetch(
        "SELECT * FROM media WHERE message_id = ANY($1::int[]) ORDER BY id",
        message_ids,
    )
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["message_id"], []).append(_media_to_dict(r))
    return out


async def _fetch_reply_tg_ids(conn, internal_ids: list[int]) -> dict[int, int]:
    """По internal reply_to_message_id вернуть маппинг id → telegram_message_id."""
    if not internal_ids:
        return {}
    rows = await conn.fetch(
        "SELECT id, telegram_message_id FROM messages WHERE id = ANY($1::int[])",
        internal_ids,
    )
    return {r["id"]: r["telegram_message_id"] for r in rows}


async def _fetch_reactions_by_messages(conn, message_ids: list[int]) -> dict[int, list[dict]]:
    """Активные реакции (removed_at IS NULL), сгруппированы по message_id."""
    if not message_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT message_id, emoji, custom_emoji_id, is_outgoing, created_at, removed_at
        FROM reactions
        WHERE message_id = ANY($1::int[]) AND removed_at IS NULL
        ORDER BY message_id, created_at
        """,
        message_ids,
    )
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["message_id"], []).append({
            "emoji": r["emoji"],
            "custom_emoji_id": r["custom_emoji_id"],
            "is_outgoing": r["is_outgoing"],
            "created_at": _iso(r["created_at"]),
            "removed_at": _iso(r["removed_at"]),
        })
    return out


async def _fetch_reply_previews(conn, internal_ids: list[int]) -> dict[int, dict]:
    """По internal reply_to_message_id отдать текст/is_outgoing оригинала."""
    if not internal_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT id, telegram_message_id, text, is_outgoing
        FROM messages WHERE id = ANY($1::int[])
        """,
        internal_ids,
    )
    return {
        r["id"]: {
            "telegram_message_id": r["telegram_message_id"],
            "text": r["text"],
            "is_outgoing": r["is_outgoing"],
        }
        for r in rows
    }


# ─────────────────────────────────────────────────────────────────────
# GET /accounts/{id}/dialogs
# ─────────────────────────────────────────────────────────────────────

@router.get("/accounts/{account_id}/dialogs")
async def list_dialogs(
    account_id: int,
    limit: int = Query(50, ge=1, le=200),
):
    """
    Список диалогов аккаунта. Сортировка — по дате последнего сообщения
    (самые свежие сверху, как в Telegram).

    Пагинация не курсорная — диалогов на один аккаунт немного, кидаем limit.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        acc = await conn.fetchrow(
            "SELECT id FROM accounts WHERE id = $1", account_id,
        )
        if acc is None:
            raise HTTPException(404, {"error": {"code": "ACCOUNT_NOT_FOUND"}})

        rows = await conn.fetch(
            """
            SELECT
                d.*,
                lm.date AS last_message_date,
                lm.text AS last_message_text,
                lm.is_outgoing AS last_message_is_outgoing
            FROM dialogs d
            LEFT JOIN LATERAL (
                SELECT date, text, is_outgoing
                FROM messages
                WHERE dialog_id = d.id
                ORDER BY date DESC, id DESC
                LIMIT 1
            ) lm ON TRUE
            WHERE d.account_id = $1
            ORDER BY lm.date DESC NULLS LAST, d.id DESC
            LIMIT $2
            """,
            account_id, limit,
        )

    dialogs = []
    for r in rows:
        d = _dialog_to_dict(r)
        d["last_message"] = {
            "date": _iso(r["last_message_date"]),
            "text": r["last_message_text"],
            "is_outgoing": r["last_message_is_outgoing"],
        } if r["last_message_date"] else None
        dialogs.append(d)

    return {"dialogs": dialogs}


# ─────────────────────────────────────────────────────────────────────
# GET /dialogs/{id}
# ─────────────────────────────────────────────────────────────────────

@router.get("/dialogs/{dialog_id}")
async def get_dialog(dialog_id: int):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                d.*,
                (SELECT COUNT(*) FROM messages WHERE dialog_id = d.id) AS messages_count,
                (SELECT COUNT(*) FROM media m
                  JOIN messages msg ON msg.id = m.message_id
                  WHERE msg.dialog_id = d.id) AS media_count
            FROM dialogs d
            WHERE d.id = $1
            """,
            dialog_id,
        )
    if row is None:
        raise HTTPException(404, {"error": {"code": "DIALOG_NOT_FOUND"}})

    out = _dialog_to_dict(row)
    out["stats"] = {
        "messages_count": row["messages_count"],
        "media_count": row["media_count"],
    }
    return out


# ─────────────────────────────────────────────────────────────────────
# DELETE /dialogs/{id}
# ─────────────────────────────────────────────────────────────────────

@router.delete("/dialogs/{dialog_id}", status_code=204)
async def delete_dialog(dialog_id: int, request: Request):
    """
    Полное жёсткое удаление диалога. Для системы собеседник станет «новым»:
    следующее сообщение от/к нему создаст dialog с пустой историей.

    Шаги:
      1. Останавливаем активную AutoChat-сессию по паре
         (account_id, telegram_user_id), если есть.
      2. Удаляем MinIO-файлы всех media этого диалога.
      3. DELETE FROM dialogs — Postgres каскадом снесёт messages →
         media/reactions/message_edits (FK CASCADE из миграции 0001).
      4. Публикуем `dialog.deleted` в шину для аудита.

    events_archive не трогаем — это исторический лог. dialog_id внутри JSONB
    `data` останется, но это валидное поведение audit-trail.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        dlg = await conn.fetchrow(
            "SELECT id, account_id, telegram_user_id, username FROM dialogs WHERE id = $1",
            dialog_id,
        )
        if dlg is None:
            raise HTTPException(404, {"error": {"code": "DIALOG_NOT_FOUND"}})

        active_session = await conn.fetchrow(
            """
            SELECT id FROM autochat_sessions
            WHERE account_id = $1 AND telegram_user_id = $2
              AND status IN ('starting', 'active', 'paused')
            LIMIT 1
            """,
            dlg["account_id"], dlg["telegram_user_id"],
        )

        media_keys = await conn.fetch(
            """
            SELECT m.storage_key
            FROM media m
            JOIN messages msg ON msg.id = m.message_id
            WHERE msg.dialog_id = $1
              AND m.storage_key IS NOT NULL
              AND m.file_deleted_at IS NULL
            """,
            dialog_id,
        )

    # 1. Останавливаем активную автосессию (если есть).
    if active_session is not None:
        autochat_service = request.app.state.autochat_service
        try:
            await autochat_service.stop_session(active_session["id"])
        except Exception:
            # Не блокируем удаление из-за проблем со stop'ом — сессия
            # будет в "stopped" после CASCADE NULL по dialog_id.
            pass

    # 2. Удаляем MinIO-файлы. Ошибки логируем, но удаление dialog'а не блокируем
    # (orphan-файлы потом подберёт cleaner или TTL bucket'а).
    for r in media_keys:
        key = r["storage_key"]
        try:
            await minio_mod.remove_object(key)
        except Exception:
            pass

    # 3. CASCADE снесёт всё связанное с dialog (messages → media/reactions/edits).
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM dialogs WHERE id = $1", dialog_id)

    # 4. Audit.
    await bus.publish(
        module=Module.API,
        type=EventType.DIALOG_DELETED,
        status=Status.SUCCESS,
        account_id=dlg["account_id"],
        data={
            "dialog_id": dialog_id,
            "telegram_user_id": dlg["telegram_user_id"],
            "username": dlg["username"],
            "media_files_attempted": len(media_keys),
            "stopped_autochat_session_id": active_session["id"] if active_session else None,
        },
    )

    return None


# ─────────────────────────────────────────────────────────────────────
# PATCH /dialogs/{id}/status — пометка оператора (UI-only)
# ─────────────────────────────────────────────────────────────────────

class DialogStatusBody(BaseModel):
    # None = «снять статус». На фронте допустимы 4 значения, но бэк
    # принимает любую короткую строку — это операторская заметка,
    # никакой логикой не используется.
    status: Optional[str] = None


@router.patch("/dialogs/{dialog_id}/status")
async def patch_dialog_user_status(dialog_id: int, body: DialogStatusBody):
    value = body.status
    if value is not None:
        value = value.strip()
        if not value:
            value = None
        elif len(value) > 32:
            raise HTTPException(400, {"error": {"code": "STATUS_TOO_LONG"}})

    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE dialogs SET user_status = $2, updated_at = NOW()
            WHERE id = $1
            RETURNING id, user_status
            """,
            dialog_id, value,
        )
    if row is None:
        raise HTTPException(404, {"error": {"code": "DIALOG_NOT_FOUND"}})
    return {"id": row["id"], "user_status": row["user_status"]}


# ─────────────────────────────────────────────────────────────────────
# GET /dialogs/{id}/messages — курсорная пагинация
# ─────────────────────────────────────────────────────────────────────

@router.get("/dialogs/{dialog_id}/messages")
async def list_dialog_messages(
    dialog_id: int,
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = None,
    direction: str = Query("backward", pattern="^(backward|forward)$"),
):
    """
    По умолчанию — direction=backward: от свежих к старым, курсор ведёт в прошлое.
    direction=forward — от старых к свежим, курсор ведёт в будущее.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        dlg = await conn.fetchrow(
            "SELECT id FROM dialogs WHERE id = $1", dialog_id,
        )
        if dlg is None:
            raise HTTPException(404, {"error": {"code": "DIALOG_NOT_FOUND"}})

        params: list[Any] = [dialog_id]
        where = "dialog_id = $1"

        if cursor:
            cur_date, cur_id = _decode_msg_cursor(cursor)
            params += [cur_date, cur_id]
            if direction == "backward":
                where += f" AND (date, id) < (${len(params) - 1}, ${len(params)})"
            else:
                where += f" AND (date, id) > (${len(params) - 1}, ${len(params)})"

        order = "DESC" if direction == "backward" else "ASC"

        sql = f"""
            SELECT * FROM messages
            WHERE {where}
            ORDER BY date {order}, id {order}
            LIMIT {limit + 1}
        """
        rows = await conn.fetch(sql, *params)

        has_more = len(rows) > limit
        rows = rows[:limit]

        if not rows:
            return {"messages": [], "next_cursor": None}

        msg_ids = [r["id"] for r in rows]
        reply_internal_ids = [
            r["reply_to_message_id"] for r in rows if r["reply_to_message_id"]
        ]

        media_by_msg = await _fetch_media_by_messages(conn, msg_ids)
        reply_tg_map = await _fetch_reply_tg_ids(conn, reply_internal_ids)
        reactions_by_msg = await _fetch_reactions_by_messages(conn, msg_ids)

    messages = [
        _message_to_dict(
            r,
            media=media_by_msg.get(r["id"], []),
            reactions=reactions_by_msg.get(r["id"], []),
            reply_to_tg=reply_tg_map.get(r["reply_to_message_id"])
                        if r["reply_to_message_id"] else None,
        )
        for r in rows
    ]

    next_cursor = None
    if has_more:
        last = rows[-1]
        next_cursor = _encode_msg_cursor(last["date"], last["id"])

    return {"messages": messages, "next_cursor": next_cursor}


# ─────────────────────────────────────────────────────────────────────
# GET /dialogs/{id}/stream — SSE
#
# Объявлен ДО POST /dialogs/{id}/read чтобы не ловить коллизий путей.
# ─────────────────────────────────────────────────────────────────────

@router.get("/dialogs/{dialog_id}/stream")
async def stream_dialog(dialog_id: int, request: Request):
    """
    SSE-поток событий по конкретному диалогу:
    message.saved и message.updated с data.dialog_id == dialog_id.

    Last-Event-ID обрабатывается — при переподключении досылает пропущенное.
    """
    last_event_id = request.headers.get("Last-Event-ID") or "$"
    interesting = {EventType.MESSAGE_SAVED, EventType.MESSAGE_UPDATED}

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
                if event.get("type") not in interesting:
                    continue
                data = event.get("data") or {}
                if data.get("dialog_id") != dialog_id:
                    continue

                yield sse_format(event="event", data=event, id=stream_id)

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
# POST /dialogs/{id}/read
# ─────────────────────────────────────────────────────────────────────

@router.post("/dialogs/{dialog_id}/read")
async def mark_dialog_read(dialog_id: int, request: Request):
    """
    Помечает весь диалог прочитанным через Telegram (wrapper.read_message).
    Воркер аккаунта должен быть запущен.

    Endpoint готов, но UI его пока не дёргает — прочтение хендлим руками,
    чтобы не мешать симулятору человечности (Этап 6).
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT account_id, telegram_user_id FROM dialogs WHERE id = $1",
            dialog_id,
        )
    if row is None:
        raise HTTPException(404, {"error": {"code": "DIALOG_NOT_FOUND"}})

    manager = request.app.state.worker_manager
    wrapper = manager.get_wrapper(row["account_id"])
    if wrapper is None:
        raise HTTPException(409, {"error": {"code": "WORKER_NOT_RUNNING"}})

    try:
        ok = await wrapper.read_message(row["telegram_user_id"])
    except SessionExpired:
        raise HTTPException(410, {"error": {"code": "SESSION_EXPIRED"}})
    except WrapperError as e:
        raise HTTPException(500, {"error": {"code": "WRAPPER_ERROR", "message": str(e)}})

    return {"ok": bool(ok)}


# ─────────────────────────────────────────────────────────────────────
# GET /messages/{id}
# ─────────────────────────────────────────────────────────────────────

@router.get("/messages/{message_id}")
async def get_message(message_id: int):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM messages WHERE id = $1", message_id,
        )
        if row is None:
            raise HTTPException(404, {"error": {"code": "MESSAGE_NOT_FOUND"}})

        media_by_msg = await _fetch_media_by_messages(conn, [row["id"]])
        reactions_by_msg = await _fetch_reactions_by_messages(conn, [row["id"]])
        reply_tg = None
        if row["reply_to_message_id"]:
            r2 = await conn.fetchrow(
                "SELECT telegram_message_id FROM messages WHERE id = $1",
                row["reply_to_message_id"],
            )
            if r2:
                reply_tg = r2["telegram_message_id"]

    return _message_to_dict(
        row,
        media=media_by_msg.get(row["id"], []),
        reactions=reactions_by_msg.get(row["id"], []),
        reply_to_tg=reply_tg,
    )


# ─────────────────────────────────────────────────────────────────────
# GET /messages/{id}/edits — история правок
# ─────────────────────────────────────────────────────────────────────

@router.get("/messages/{message_id}/edits")
async def list_message_edits(message_id: int):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM messages WHERE id = $1", message_id,
        )
        if not exists:
            raise HTTPException(404, {"error": {"code": "MESSAGE_NOT_FOUND"}})
        rows = await conn.fetch(
            """
            SELECT id, message_id, old_text, edited_at
            FROM message_edits
            WHERE message_id = $1
            ORDER BY edited_at ASC, id ASC
            """,
            message_id,
        )
    return [
        {
            "id": r["id"],
            "message_id": r["message_id"],
            "old_text": r["old_text"],
            "edited_at": _iso(r["edited_at"]),
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────
# POST /accounts/{id}/messages — отправить сообщение
# ─────────────────────────────────────────────────────────────────────

class SendMessageBody(BaseModel):
    dialog_id: int
    text: str
    reply_to_message_id: Optional[int] = None  # внутренний id нашего сообщения


SEND_WAIT_TIMEOUT = 15.0  # сек на ожидание записи в БД после отправки
SEND_WAIT_POLL = 0.1


@router.post("/accounts/{account_id}/messages")
async def send_message(
    account_id: int,
    body: SendMessageBody,
    request: Request,
):
    """
    Отправить сообщение через воркер. Синхронно — возвращаем готовый объект
    только после того как история его записала (пришёл message.saved и
    запись появилась в БД).
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        # Валидация: диалог существует и принадлежит аккаунту
        dlg = await conn.fetchrow(
            "SELECT id, account_id, telegram_user_id FROM dialogs WHERE id = $1",
            body.dialog_id,
        )
        if dlg is None:
            raise HTTPException(404, {"error": {"code": "DIALOG_NOT_FOUND"}})
        if dlg["account_id"] != account_id:
            raise HTTPException(400, {"error": {"code": "DIALOG_ACCOUNT_MISMATCH"}})

        # Reply_to — резолвим внутренний id в telegram_message_id
        reply_to_tg: int | None = None
        if body.reply_to_message_id is not None:
            r = await conn.fetchrow(
                "SELECT telegram_message_id FROM messages WHERE id = $1 AND dialog_id = $2",
                body.reply_to_message_id, body.dialog_id,
            )
            if r is None:
                raise HTTPException(400, {"error": {"code": "INVALID_REPLY"}})
            reply_to_tg = r["telegram_message_id"]

    # Берём живой wrapper
    manager = request.app.state.worker_manager
    wrapper = manager.get_wrapper(account_id)
    if wrapper is None:
        raise HTTPException(409, {"error": {"code": "WORKER_NOT_RUNNING"}})

    try:
        sent = await wrapper.send_message(
            dlg["telegram_user_id"], body.text, reply_to=reply_to_tg,
        )
    except SessionExpired:
        raise HTTPException(410, {"error": {"code": "SESSION_EXPIRED"}})
    except WrapperError as e:
        raise HTTPException(500, {"error": {"code": "WRAPPER_ERROR", "message": str(e)}})

    tg_msg_id = sent.get("telegram_message_id")
    if tg_msg_id is None:
        raise HTTPException(500, {"error": {"code": "SEND_FAILED", "message": "no telegram_message_id"}})

        # Telethon НЕ триггерит NewMessage handler для сообщений отправленных
    # этим же клиентом — эхо приходит только с других устройств. Поэтому
    # здесь сами публикуем message.received, чтобы модуль истории его
    # записал. Формат — как у воркера.
    from core.events import EventType as _ET, Module as _MOD, Status as _ST
    await bus.publish(
        module=_MOD.WRAPPER,
        type=_ET.MESSAGE_RECEIVED,
        status=_ST.SUCCESS,
        account_id=account_id,
        data={
            "telegram_message_id": tg_msg_id,
            "telegram_user_id": dlg["telegram_user_id"],
            "is_outgoing": True,
            "date": sent.get("date") or bus.now_utc(),
            "text": body.text,
            "reply_to_telegram_message_id": reply_to_tg,
            "forward_from": None,
            "media_group_id": None,
            "peer_profile": None,
            "media": [],
        },
    )

    # Ждём пока модуль истории запишет его (через echo в NewMessage handler)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + SEND_WAIT_TIMEOUT

    async with pool.acquire() as conn:
        while loop.time() < deadline:
            row = await conn.fetchrow(
                "SELECT * FROM messages WHERE dialog_id = $1 AND telegram_message_id = $2",
                body.dialog_id, tg_msg_id,
            )
            if row is not None:
                media_by_msg = await _fetch_media_by_messages(conn, [row["id"]])
                reply_tg = None
                if row["reply_to_message_id"]:
                    r2 = await conn.fetchrow(
                        "SELECT telegram_message_id FROM messages WHERE id = $1",
                        row["reply_to_message_id"],
                    )
                    if r2:
                        reply_tg = r2["telegram_message_id"]
                return _message_to_dict(
                    row,
                    media=media_by_msg.get(row["id"], []),
                    reactions=[],
                    reply_to_tg=reply_tg,
                )
            await asyncio.sleep(SEND_WAIT_POLL)

    # Не дождались — но Telegram уже принял сообщение. Возвращаем 504.
    raise HTTPException(
        504,
        {
            "error": {
                "code": "SEND_TIMEOUT",
                "message": (
                    f"Сообщение отправлено в Telegram (tg_msg_id={tg_msg_id}), "
                    "но запись в БД не появилась за отведённое время. "
                    "Через минуту обнови ленту."
                ),
                "details": {"telegram_message_id": tg_msg_id},
            }
        },
    )
