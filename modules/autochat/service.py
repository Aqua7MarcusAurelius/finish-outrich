"""
AutoChatService — центральный оркестратор модуля AutoChat.

Задачи:
  1. Фоновый consumer шины (group "autochat-worker"): раздаёт события
     активным сессиям (message.saved, message.updated, dialog.typing_observed).
  2. Публичный API — create_session / list_sessions / get_session /
     stop_session. Вызывается из роутера (modules/autochat/routes.py).
  3. Восстановление при рестарте — не реализовано в MVP (см. docs/autochat.md);
     активные сессии после падения остаются в БД, поднимаются вручную через
     ре-создание. Заложен хук _restore_on_start() для второго захода.

См. docs/autochat.md.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable

from core import bus, db
from core.events import EventType, Module, Status
from core.openrouter import OpenRouterError

from modules.worker.wrapper import (
    SessionExpired as WrapperSessionExpired,
    UsernameNotFound as WrapperUsernameNotFound,
    UsernameUnavailable as WrapperUsernameUnavailable,
    WrapperError,
)

from .errors import (
    CannotWrite,
    GenerationFailed,
    SessionAlreadyActive,
    SessionExpired as AutoSessionExpired,
    SessionNotFound,
    UsernameNotFoundError,
    UsernameUnavailableError,
    WorkerNotRunning,
)
from .generation import build_initial_messages, sanitize_initial_response
from .session import AutoChatSession, _call_llm_with_retries, _get_setting_int, _now

log = logging.getLogger(__name__)

AUTOCHAT_GROUP = "autochat-worker"
AUTOCHAT_CONSUMER = "autochat-worker-1"

# Типы событий которые нас интересуют. Остальное — молча ack'ается.
_INTERESTING_TYPES = {
    EventType.MESSAGE_SAVED,
    EventType.MESSAGE_UPDATED,
    EventType.DIALOG_TYPING_OBSERVED,
}


class AutoChatService:
    """
    Фоновый сервис. Создаётся в lifespan и запускается как asyncio.Task.
    """

    def __init__(self, *, get_wrapper: Callable[[int], Any]):
        self._get_wrapper = get_wrapper

        # Индексы сессий
        self._sessions_by_id: dict[int, AutoChatSession] = {}
        self._by_dialog: dict[tuple[int, int], int] = {}         # (account_id, dialog_id) → session_id
        self._by_tg_user: dict[tuple[int, int], int] = {}        # (account_id, telegram_user_id) → session_id

        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

    # ─────────────────────────────────────────────────────────────────
    # Жизненный цикл
    # ─────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        await bus.ensure_group(AUTOCHAT_GROUP)
        await self._restore_active_sessions()
        log.info("AutoChatService loop started")

        while not self._stop_event.is_set():
            try:
                batch = await bus.read_group(
                    AUTOCHAT_GROUP, AUTOCHAT_CONSUMER,
                    count=50, block_ms=5000,
                )
                if not batch:
                    continue

                ack_ids: list[str] = []
                for stream_id, event in batch:
                    try:
                        await self._dispatch(event)
                        ack_ids.append(stream_id)
                        await bus.record_success(AUTOCHAT_GROUP, stream_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.exception(
                            "autochat: dispatch failed for %s (type=%s)",
                            event.get("id"), event.get("type"),
                        )
                        force_ack = await bus.record_failure(
                            AUTOCHAT_GROUP, stream_id, event, e,
                        )
                        if force_ack:
                            ack_ids.append(stream_id)

                if ack_ids:
                    await bus.ack_group(AUTOCHAT_GROUP, ack_ids)

            except asyncio.CancelledError:
                log.info("AutoChatService loop cancelled")
                raise
            except Exception:
                log.exception("AutoChatService loop error")
                await asyncio.sleep(1)

    async def _restore_active_sessions(self) -> None:
        """
        Поднять из БД все сессии со status='active' после рестарта приложения.

        Всегда форсим in_chat=False — состояние enter/idle таймеров не
        переживает рестарт, следующий inbound нормально запустит enter-timer
        (см. docs/autochat.md → «Восстановление при рестарте»).

        Воркер аккаунта может быть ещё не запущен — это ок, сессия будет
        ждать события. Когда пользователь сделает POST /workers/{id}/start
        и придёт первое message.saved — state machine оживёт.
        """
        pool = db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM autochat_sessions WHERE status = 'active'",
            )
        if not rows:
            return

        restored = 0
        for row in rows:
            data = dict(row)
            data["in_chat"] = False  # форсим сброс на рестарте
            try:
                session = AutoChatSession(row=data, get_wrapper=self._get_wrapper)
                async with self._lock:
                    self._sessions_by_id[session.id] = session
                    self._by_tg_user[(session.account_id, session.telegram_user_id)] = session.id
                    if session.dialog_id is not None:
                        self._by_dialog[(session.account_id, session.dialog_id)] = session.id
                await session.start()
                restored += 1
            except Exception:
                log.exception(
                    "autochat: failed to restore session %s", row["id"],
                )
        if restored:
            log.info("autochat: restored %d active session(s)", restored)

    async def stop(self) -> None:
        """Остановить consumer и все активные сессии."""
        self._stop_event.set()
        async with self._lock:
            sessions = list(self._sessions_by_id.values())
        for s in sessions:
            try:
                await s.stop(reason="service_shutdown")
            except Exception:
                log.exception("autochat: failed to stop session %s on shutdown", s.id)

    # ─────────────────────────────────────────────────────────────────
    # Dispatcher
    # ─────────────────────────────────────────────────────────────────

    async def _dispatch(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype not in _INTERESTING_TYPES:
            return

        account_id = event.get("account_id")
        data = event.get("data") or {}

        if etype == EventType.MESSAGE_SAVED:
            await self._on_message_saved(account_id, data)
        elif etype == EventType.MESSAGE_UPDATED:
            await self._on_message_updated(account_id, data)
        elif etype == EventType.DIALOG_TYPING_OBSERVED:
            await self._on_typing(account_id, data)

    async def _on_message_saved(
        self, account_id: int | None, data: dict[str, Any],
    ) -> None:
        if account_id is None:
            return
        dialog_id = data.get("dialog_id")
        if dialog_id is None:
            return

        # Достаём telegram_user_id через БД (в message.saved его нет)
        telegram_user_id = await self._resolve_tg_user(dialog_id)
        if telegram_user_id is None:
            return

        session = None
        async with self._lock:
            # Сначала пытаемся по (account_id, dialog_id)
            sid = self._by_dialog.get((account_id, dialog_id))
            if sid is None:
                # Фолбэк: сессия ещё не привязана к dialog_id (только что создана)
                sid = self._by_tg_user.get((account_id, telegram_user_id))
                if sid is not None:
                    # Привязываем dialog_id к сессии
                    self._by_dialog[(account_id, dialog_id)] = sid
            if sid is not None:
                session = self._sessions_by_id.get(sid)

        if session is None:
            return

        if session.dialog_id is None:
            try:
                await session.set_dialog_id(dialog_id)
            except Exception:
                log.exception("autochat: failed to persist dialog_id for session %s", session.id)

        is_outgoing = bool(data.get("is_outgoing", False))

        payload = {
            "message_id": data.get("message_id"),
            "telegram_message_id": data.get("telegram_message_id"),
            "dialog_id": dialog_id,
            "is_outgoing": is_outgoing,
            "date": _now(),   # message.saved не содержит сырой date; используем now
        }
        kind = "outbound" if is_outgoing else "inbound"
        await session.handle_event(kind, payload)

    async def _on_message_updated(
        self, account_id: int | None, data: dict[str, Any],
    ) -> None:
        if account_id is None:
            return
        dialog_id = data.get("dialog_id")
        if dialog_id is None:
            return
        async with self._lock:
            sid = self._by_dialog.get((account_id, dialog_id))
            session = self._sessions_by_id.get(sid) if sid is not None else None
        if session is None:
            return
        await session.handle_event("media_updated", {
            "message_id": data.get("message_id"),
            "media_id": data.get("media_id"),
            "field": data.get("field"),
            "status": data.get("status"),
        })

    async def _on_typing(
        self, account_id: int | None, data: dict[str, Any],
    ) -> None:
        if account_id is None:
            return
        tg_user_id = data.get("telegram_user_id")
        if tg_user_id is None:
            return
        async with self._lock:
            sid = self._by_tg_user.get((account_id, int(tg_user_id)))
            session = self._sessions_by_id.get(sid) if sid is not None else None
        if session is None:
            return
        await session.handle_event("typing", {"telegram_user_id": tg_user_id})

    # ─────────────────────────────────────────────────────────────────
    # API: create_session
    # ─────────────────────────────────────────────────────────────────

    async def create_session(
        self,
        *,
        account_id: int,
        username: str,
        system_prompt: str,
        initial_prompt: str,
    ) -> dict[str, Any]:
        """
        Синхронный флоу:
          1) проверка воркера;
          2) resolve @username → telegram_user_id;
          3) проверка "нет активной сессии на пару";
          4) LLM: initial сообщение;
          5) INSERT сессии (starting);
          6) wrapper.send_message + publish message.received;
          7) UPDATE status=active, regist indexes, session.start();
          8) publish autochat.started + autochat.initial_sent;
          9) возврат row.
        """
        wrapper = self._get_wrapper(account_id)
        if wrapper is None:
            raise WorkerNotRunning()

        # resolve
        try:
            profile = await wrapper.resolve_username(username)
        except WrapperSessionExpired as e:
            raise AutoSessionExpired(str(e)) from e
        except WrapperUsernameNotFound as e:
            raise UsernameNotFoundError(str(e)) from e
        except WrapperUsernameUnavailable as e:
            raise UsernameUnavailableError(str(e)) from e
        except WrapperError as e:
            raise UsernameUnavailableError(str(e)) from e

        telegram_user_id = int(profile["telegram_user_id"])
        cleaned_username = (username or "").strip().lstrip("@")

        # Проверка уникальности до запроса в LLM — сэкономит на резолве дубля
        pool = db.get_pool()
        async with pool.acquire() as conn:
            exists = await conn.fetchrow(
                """
                SELECT id FROM autochat_sessions
                WHERE account_id = $1 AND telegram_user_id = $2
                  AND status IN ('starting','active','paused')
                LIMIT 1
                """,
                account_id, telegram_user_id,
            )
        if exists:
            raise SessionAlreadyActive()

        # LLM: initial
        initial_messages = build_initial_messages(
            system_prompt=system_prompt,
            initial_prompt=initial_prompt,
            now=_now(),
        )
        try:
            retries = await _get_setting_int("autochat.openrouter_retries")
            raw = await _call_llm_with_retries(initial_messages, retries=retries)
        except OpenRouterError as e:
            raise GenerationFailed(str(e)) from e

        initial_text = sanitize_initial_response(raw).strip()
        if not initial_text:
            raise GenerationFailed("LLM вернул пустое первое сообщение")

        # INSERT сессии (starting — на случай если send провалится)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO autochat_sessions (
                    account_id, telegram_user_id, target_username,
                    system_prompt, initial_prompt, initial_sent_text,
                    status
                ) VALUES ($1, $2, $3, $4, $5, $6, 'starting')
                ON CONFLICT (account_id, telegram_user_id)
                    WHERE status IN ('active','paused','starting')
                    DO NOTHING
                RETURNING *
                """,
                account_id, telegram_user_id, cleaned_username,
                system_prompt, initial_prompt, initial_text,
            )
        if row is None:
            # Гонка — другой запрос успел вставить такую же пару первым
            raise SessionAlreadyActive()

        session_id = row["id"]

        # publish autochat.started
        started_ev = await bus.publish(
            module=Module.AUTOCHAT,
            type=EventType.AUTOCHAT_STARTED,
            status=Status.SUCCESS,
            account_id=account_id,
            data={
                "session_id": session_id,
                "username": cleaned_username,
                "telegram_user_id": telegram_user_id,
            },
        )

        # Send первого сообщения
        try:
            sent = await wrapper.send_message(telegram_user_id, initial_text)
        except WrapperSessionExpired as e:
            await self._mark_failed(session_id, "session_expired")
            raise AutoSessionExpired(str(e)) from e
        except Exception as e:
            await self._mark_failed(session_id, f"send_failed: {e}")
            raise CannotWrite(str(e)) from e

        tg_msg_id = sent.get("telegram_message_id")

        # Публикуем message.received (история запишет; history создаст dialog)
        if tg_msg_id is not None:
            await bus.publish(
                module=Module.WRAPPER,
                type=EventType.MESSAGE_RECEIVED,
                status=Status.SUCCESS,
                account_id=account_id,
                data={
                    "telegram_message_id": tg_msg_id,
                    "telegram_user_id": telegram_user_id,
                    "is_outgoing": True,
                    "date": sent.get("date") or bus.now_utc(),
                    "text": initial_text,
                    "reply_to_telegram_message_id": None,
                    "forward_from": None,
                    "media_group_id": None,
                    "peer_profile": profile,
                    "media": [],
                },
            )

        # initial_sent event
        await bus.publish(
            module=Module.AUTOCHAT,
            type=EventType.AUTOCHAT_INITIAL_SENT,
            status=Status.SUCCESS,
            parent_id=started_ev["id"],
            account_id=account_id,
            data={
                "session_id": session_id,
                "telegram_message_id": tg_msg_id,
                "text": initial_text,
            },
        )

        # Промоушен до active + регистрация сессии
        async with pool.acquire() as conn:
            final_row = await conn.fetchrow(
                """
                UPDATE autochat_sessions SET
                    status = 'active',
                    last_our_activity_at = NOW(),
                    last_any_message_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                session_id,
            )

        session = AutoChatSession(
            row=dict(final_row),
            get_wrapper=self._get_wrapper,
        )
        async with self._lock:
            self._sessions_by_id[session_id] = session
            self._by_tg_user[(account_id, telegram_user_id)] = session_id
        await session.start()

        return _row_to_dict(final_row)

    async def stop_session(self, session_id: int) -> dict[str, Any]:
        async with self._lock:
            session = self._sessions_by_id.pop(session_id, None)
            # Почистим индексы
            for key, sid in list(self._by_dialog.items()):
                if sid == session_id:
                    self._by_dialog.pop(key, None)
            for key, sid in list(self._by_tg_user.items()):
                if sid == session_id:
                    self._by_tg_user.pop(key, None)

        if session is not None:
            await session.stop(reason="manual_stop")

        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE autochat_sessions SET
                    status = CASE
                        WHEN status IN ('failed','stopped') THEN status
                        ELSE 'stopped'
                    END,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                session_id,
            )
        if row is None:
            raise SessionNotFound()

        await bus.publish(
            module=Module.AUTOCHAT,
            type=EventType.AUTOCHAT_SESSION_STOPPED,
            status=Status.SUCCESS,
            account_id=row["account_id"],
            data={"session_id": session_id},
        )
        return _row_to_dict(row)

    async def list_sessions(
        self, *, account_id: int | None = None, status: str | None = None,
    ) -> list[dict[str, Any]]:
        pool = db.get_pool()
        params: list[Any] = []
        where: list[str] = []
        if account_id is not None:
            params.append(account_id)
            where.append(f"account_id = ${len(params)}")
        if status is not None:
            params.append(status)
            where.append(f"status = ${len(params)}")
        sql = "SELECT * FROM autochat_sessions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT 200"
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [_row_to_dict(r) for r in rows]

    async def get_session(self, session_id: int) -> dict[str, Any]:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM autochat_sessions WHERE id = $1", session_id,
            )
        if row is None:
            raise SessionNotFound()
        return _row_to_dict(row)

    # ─────────────────────────────────────────────────────────────────
    # Вспомогательные запросы
    # ─────────────────────────────────────────────────────────────────

    async def _resolve_tg_user(self, dialog_id: int) -> int | None:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT telegram_user_id FROM dialogs WHERE id = $1", dialog_id,
            )
        return int(row["telegram_user_id"]) if row else None

    async def _mark_failed(self, session_id: int, reason: str) -> None:
        pool = db.get_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE autochat_sessions SET
                        status = 'failed',
                        last_error = $2,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    session_id, reason[:500],
                )
        except Exception:
            log.exception("autochat: failed to mark session %s as failed", session_id)


# ─────────────────────────────────────────────────────────────────────
# Сериализация строк
# ─────────────────────────────────────────────────────────────────────

def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "dialog_id": row["dialog_id"],
        "telegram_user_id": row["telegram_user_id"],
        "target_username": row["target_username"],
        "system_prompt": row["system_prompt"],
        "initial_prompt": row["initial_prompt"],
        "initial_sent_text": row["initial_sent_text"],
        "status": row["status"],
        "in_chat": row["in_chat"],
        "last_our_activity_at": _iso(row["last_our_activity_at"]),
        "last_their_message_at": _iso(row["last_their_message_at"]),
        "last_any_message_at": _iso(row["last_any_message_at"]),
        "last_error": row["last_error"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }
