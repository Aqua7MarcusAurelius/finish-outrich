"""
Модуль истории.

Единственный writer в таблицы dialogs, messages, media, reactions, message_edits.

Слушает шину событий через consumer group "history-writer":
  - message.received    → upsert dialogs, insert messages+media, publish message.saved
  - transcription.done  → update media.transcription, publish message.updated
  - description.done    → update media.description, publish message.updated
  - (остальные события) → молча ack'аются, пропускаются

См. docs/history.md.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from core import bus, db
from core.events import EventType, Module, Status

log = logging.getLogger(__name__)

HISTORY_GROUP = "history-writer"
HISTORY_CONSUMER = "history-writer-1"

# Типы медиа для расчёта флагов has_*
_AUDIO_TYPES = {"voice", "audio", "video_note"}
_IMAGE_TYPES = {"photo", "sticker", "gif"}
_VIDEO_TYPES = {"video", "video_note"}


def _media_statuses(media_type: str) -> tuple[str, str]:
    """
    Начальные значения (transcription_status, description_status)
    для media-записи при вставке.
    """
    if media_type in ("voice", "audio"):
        return ("pending", "none")
    if media_type in ("video", "video_note"):
        return ("pending", "pending")
    if media_type in ("photo", "sticker", "gif", "document"):
        return ("none", "pending")
    return ("none", "none")


def _compute_flags(text: str | None, media: list[dict]) -> dict[str, bool]:
    types = {m.get("type") for m in media if m.get("type")}
    return {
        "has_text": bool(text),
        "has_audio": bool(types & _AUDIO_TYPES),
        "has_image": bool(types & _IMAGE_TYPES),
        "has_video": bool(types & _VIDEO_TYPES),
        "has_document": "document" in types,
    }


def _parse_dt(value: Any) -> datetime | None:
    """ISO-строка → datetime. Уже datetime — как есть. Остальное — None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


class HistoryService:
    """
    Consumer-loop модуля истории. Живёт как фоновая задача в lifespan.
    """

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        await bus.ensure_group(HISTORY_GROUP)
        log.info("HistoryService loop started")

        while not self._stop_event.is_set():
            try:
                batch = await bus.read_group(
                    HISTORY_GROUP, HISTORY_CONSUMER,
                    count=50, block_ms=5000,
                )
                if not batch:
                    continue

                ack_ids: list[str] = []
                for stream_id, event in batch:
                    try:
                        await self._handle(event)
                        ack_ids.append(stream_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception(
                            "history: failed to handle event %s (type=%s)",
                            event.get("id"), event.get("type"),
                        )
                        # Не ack'аем — переобработка в следующем прогоне.
                        # Для message.received защита от задваивания обеспечена
                        # уникальным индексом (dialog_id, telegram_message_id).

                if ack_ids:
                    await bus.ack_group(HISTORY_GROUP, ack_ids)

            except asyncio.CancelledError:
                log.info("HistoryService loop cancelled")
                raise
            except Exception:
                log.exception("HistoryService loop error")
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stop_event.set()

    # ─── Dispatcher ───────────────────────────────────────────────

    async def _handle(self, event: dict) -> None:
        et = event.get("type")
        if et == EventType.MESSAGE_RECEIVED:
            await self._on_message_received(event)
        elif et == EventType.TRANSCRIPTION_DONE:
            await self._on_transcription_done(event)
        elif et == EventType.DESCRIPTION_DONE:
            await self._on_description_done(event)
        # Остальные события нас не интересуют — молча ack'аются вызывающим.

    # ─── message.received ─────────────────────────────────────────

    async def _on_message_received(self, event: dict) -> None:
        data = event.get("data") or {}
        account_id = event.get("account_id")
        if account_id is None:
            log.warning("history: message.received without account_id, skip")
            return

        telegram_user_id = data.get("telegram_user_id")
        telegram_message_id = data.get("telegram_message_id")
        if telegram_user_id is None or telegram_message_id is None:
            log.warning(
                "history: missing tg ids in event %s, skip", event.get("id"),
            )
            return

        text = data.get("text")
        media_list: list[dict] = data.get("media") or []
        is_outgoing = bool(data.get("is_outgoing", False))
        msg_date = _parse_dt(data.get("date")) or bus.now_utc()
        media_group_id = data.get("media_group_id")
        peer_profile = data.get("peer_profile")

        fwd = data.get("forward_from") or {}
        fwd_user_id = fwd.get("user_id") if fwd else None
        fwd_chat_id = fwd.get("chat_id") if fwd else None
        fwd_name = fwd.get("name") if fwd else None
        fwd_date = _parse_dt(fwd.get("date")) if fwd else None

        reply_to_tg = data.get("reply_to_telegram_message_id")

        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. Upsert dialog
                dialog_id = await self._upsert_dialog(
                    conn, account_id, telegram_user_id, peer_profile,
                )

                # 2. Резолв reply → внутренний message.id (если ссылка на уже записанное)
                reply_internal_id = None
                if reply_to_tg is not None:
                    row = await conn.fetchrow(
                        "SELECT id FROM messages "
                        "WHERE dialog_id=$1 AND telegram_message_id=$2",
                        dialog_id, reply_to_tg,
                    )
                    if row:
                        reply_internal_id = row["id"]

                # 3. Insert message (дубль по unique-индексу пропускаем)
                message_row = await conn.fetchrow(
                    """
                    INSERT INTO messages (
                        dialog_id, telegram_message_id, is_outgoing, type, date, text,
                        reply_to_message_id,
                        forward_from_user_id, forward_from_name,
                        forward_from_chat_id, forward_date,
                        media_group_id
                    ) VALUES ($1, $2, $3, 'regular', $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (dialog_id, telegram_message_id) DO NOTHING
                    RETURNING id
                    """,
                    dialog_id, telegram_message_id, is_outgoing, msg_date, text,
                    reply_internal_id,
                    fwd_user_id, fwd_name, fwd_chat_id, fwd_date,
                    media_group_id,
                )

                if message_row is None:
                    # Дубликат — уже обрабатывали (нагоном или повтором). Молча выходим.
                    log.debug(
                        "history: duplicate dialog=%s tg_msg=%s, skip",
                        dialog_id, telegram_message_id,
                    )
                    return

                message_id = message_row["id"]

                # 4. Insert media
                saved_media: list[dict] = []
                for media in media_list:
                    media_type = media.get("type") or "document"
                    tstatus, dstatus = _media_statuses(media_type)
                    media_row = await conn.fetchrow(
                        """
                        INSERT INTO media (
                            message_id, type, file_name, telegram_file_id, storage_key,
                            mime_type, file_size, duration, width, height,
                            transcription_status, description_status, downloaded_at
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW()
                        )
                        RETURNING id
                        """,
                        message_id, media_type,
                        media.get("file_name"),
                        media.get("telegram_file_id"),
                        media.get("storage_key"),
                        media.get("mime_type"),
                        media.get("file_size"),
                        media.get("duration"),
                        media.get("width"),
                        media.get("height"),
                        tstatus, dstatus,
                    )
                    saved_media.append({
                        "media_id": media_row["id"],
                        "type": media_type,
                        "storage_key": media.get("storage_key"),
                        "mime_type": media.get("mime_type"),
                        "duration": media.get("duration"),
                    })

        # 5. Публикуем message.saved — уже вне транзакции, чтобы к моменту
        # доставки события медиа-модулям запись точно закоммичена.
        flags = _compute_flags(text, media_list)
        await bus.publish(
            module=Module.HISTORY,
            type=EventType.MESSAGE_SAVED,
            status=Status.SUCCESS,
            parent_id=event.get("id"),
            account_id=account_id,
            data={
                "message_id": message_id,
                "telegram_message_id": telegram_message_id,
                "dialog_id": dialog_id,
                "is_outgoing": is_outgoing,
                **flags,
                "media": saved_media,
            },
        )

    async def _upsert_dialog(
        self,
        conn: Any,
        account_id: int,
        telegram_user_id: int,
        profile: dict | None,
    ) -> int:
        """
        Upsert диалога. Если peer_profile есть — обновляем поля профиля через
        COALESCE (не перетираем существующие данные NULL-ом), флаги is_bot/is_contact
        обновляем жёстко. Если peer_profile нет — просто освежаем updated_at.
        """
        if profile is not None:
            row = await conn.fetchrow(
                """
                INSERT INTO dialogs (
                    account_id, telegram_user_id, username, first_name, last_name,
                    phone, is_bot, is_contact, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                ON CONFLICT (account_id, telegram_user_id) DO UPDATE SET
                    username    = COALESCE(EXCLUDED.username,    dialogs.username),
                    first_name  = COALESCE(EXCLUDED.first_name,  dialogs.first_name),
                    last_name   = COALESCE(EXCLUDED.last_name,   dialogs.last_name),
                    phone       = COALESCE(EXCLUDED.phone,       dialogs.phone),
                    is_bot      = EXCLUDED.is_bot,
                    is_contact  = EXCLUDED.is_contact,
                    updated_at  = NOW()
                RETURNING id
                """,
                account_id, telegram_user_id,
                profile.get("username"),
                profile.get("first_name"),
                profile.get("last_name"),
                profile.get("phone"),
                bool(profile.get("is_bot", False)),
                bool(profile.get("is_contact", False)),
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO dialogs (account_id, telegram_user_id, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (account_id, telegram_user_id) DO UPDATE SET
                    updated_at = NOW()
                RETURNING id
                """,
                account_id, telegram_user_id,
            )
        return row["id"]

    # ─── transcription.done / description.done ────────────────────

    async def _on_transcription_done(self, event: dict) -> None:
        """
        Обновить media.transcription + статус, опубликовать message.updated.
        В message.updated кладём dialog_id — чтобы SSE-фильтр на /dialogs/*/stream
        мог его отсечь.
        """
        data = event.get("data") or {}
        media_id = data.get("media_id")
        if media_id is None:
            log.warning("transcription.done without media_id, skip")
            return

        text = data.get("text")
        status = data.get("status") or "done"  # done / failed

        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH upd AS (
                    UPDATE media SET
                        transcription = $1,
                        transcription_status = $2
                    WHERE id = $3
                    RETURNING message_id
                )
                SELECT u.message_id, m.dialog_id
                FROM upd u JOIN messages m ON m.id = u.message_id
                """,
                text, status, media_id,
            )

        if row is None:
            log.warning("transcription.done: media_id=%s not found", media_id)
            return

        await bus.publish(
            module=Module.HISTORY,
            type=EventType.MESSAGE_UPDATED,
            status=Status.SUCCESS,
            parent_id=event.get("id"),
            account_id=event.get("account_id"),
            data={
                "message_id": row["message_id"],
                "dialog_id": row["dialog_id"],
                "media_id": media_id,
                "field": "transcription",
                "status": status,
            },
        )

    async def _on_description_done(self, event: dict) -> None:
        """Аналогично _on_transcription_done, только поле description."""
        data = event.get("data") or {}
        media_id = data.get("media_id")
        if media_id is None:
            log.warning("description.done without media_id, skip")
            return

        text = data.get("text")
        status = data.get("status") or "done"

        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH upd AS (
                    UPDATE media SET
                        description = $1,
                        description_status = $2
                    WHERE id = $3
                    RETURNING message_id
                )
                SELECT u.message_id, m.dialog_id
                FROM upd u JOIN messages m ON m.id = u.message_id
                """,
                text, status, media_id,
            )

        if row is None:
            log.warning("description.done: media_id=%s not found", media_id)
            return

        await bus.publish(
            module=Module.HISTORY,
            type=EventType.MESSAGE_UPDATED,
            status=Status.SUCCESS,
            parent_id=event.get("id"),
            account_id=event.get("account_id"),
            data={
                "message_id": row["message_id"],
                "dialog_id": row["dialog_id"],
                "media_id": media_id,
                "field": "description",
                "status": status,
            },
        )
