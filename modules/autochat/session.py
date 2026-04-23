"""
AutoChatSession — одна активная автопереписка.

Держит состояние (in_chat, last_* timestamps) и три asyncio-таска:
  1. state_loop    — читает входящие события из внутренней очереди и
                     двигает state machine (InChat 0/1, enter-timer,
                     idle-exit, bump planner'а).
  2. planner_loop  — ждёт reply-timer (30с тишины), проверяет готовность
                     медиа, вызывает OpenRouter и кладёт сегменты в очередь.
  3. sender_loop   — забирает сегменты из очереди, печатает (typing+пауза)
                     и отправляет через wrapper. Сериализует отправку.

События приходят через handle_event() — туда их кладёт AutoChatService,
роутящий шину.

См. docs/autochat.md.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any, Callable

from core import bus, db
from core.events import EventType, Module, Status
from core.openrouter import OpenRouterError, chat_completion

from .errors import SessionExpired as AutoSessionExpired
from .generation import (
    build_conversation_context,
    parse_segments,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Дефолты настроек — используются если в settings нет записи
# ─────────────────────────────────────────────────────────────────────

_DEFAULTS: dict[str, int] = {
    "autochat.enter_delay_short_sec": 15,
    "autochat.enter_delay_mid_sec":   60,
    "autochat.enter_delay_long_sec":  120,
    "autochat.idle_leave_sec":        180,
    "autochat.reply_timer_sec":       30,
    "autochat.openrouter_retries":    2,
    "autochat.typing_ms_per_char":    40,
}


async def _get_setting_int(key: str) -> int:
    default = _DEFAULTS.get(key, 0)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT value FROM settings WHERE key=$1", key,
        )
    try:
        return int(val) if val is not None else default
    except Exception:
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# Внутренние типы событий для state_loop
# ─────────────────────────────────────────────────────────────────────

EV_INBOUND = "inbound"            # inbound message.saved
EV_OUTBOUND = "outbound"          # наше исходящее (is_outgoing=True)
EV_MEDIA_UPDATED = "media_updated"  # message.updated с dialog_id == наш
EV_TYPING = "typing"              # dialog.typing_observed с user_id == наш


# ─────────────────────────────────────────────────────────────────────

class AutoChatSession:
    """
    Живёт пока status=active. Управляется AutoChatService.

    Не пишет в таблицы owned другими модулями — только в autochat_sessions
    (обновление last_* полей, in_chat, status, last_error).

    Отправка реальных сообщений через wrapper — один за раз (send_lock),
    параллельно может идти новая генерация.
    """

    def __init__(
        self,
        *,
        row: dict[str, Any],
        get_wrapper: Callable[[int], Any],
    ):
        # Основные поля из БД
        self.id: int = row["id"]
        self.account_id: int = row["account_id"]
        self.dialog_id: int | None = row.get("dialog_id")
        self.telegram_user_id: int = row["telegram_user_id"]
        self.target_username: str = row["target_username"]
        self.system_prompt: str = row["system_prompt"]

        self._get_wrapper = get_wrapper

        # Текущее состояние (синхронизируется с БД на ключевых точках)
        self.in_chat: bool = bool(row.get("in_chat", False))
        self.last_our_activity_at: datetime | None = row.get("last_our_activity_at")
        self.last_their_message_at: datetime | None = row.get("last_their_message_at")
        self.last_any_message_at: datetime | None = row.get("last_any_message_at")

        # Внутренние очереди / флаги
        self._queue: asyncio.Queue = asyncio.Queue()
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._planner_bump: asyncio.Event = asyncio.Event()
        self._media_ready: asyncio.Event = asyncio.Event()

        # Запущенные таски (заполняются в start())
        self._state_task: asyncio.Task | None = None
        self._planner_task: asyncio.Task | None = None
        self._sender_task: asyncio.Task | None = None

        # Вспомогательные таймеры
        self._enter_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None

        # Inbound сообщения, ждущие готовности медиа (message_id → True)
        self._pending_media_msg_ids: set[int] = set()

        self._stopped: asyncio.Event = asyncio.Event()

    # ─────────────────────────────────────────────────────────────────
    # Жизненный цикл
    # ─────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Запустить три фоновых таска."""
        log.info(
            "autochat session %s: start (account=%s tg_user=%s)",
            self.id, self.account_id, self.telegram_user_id,
        )
        self._state_task = asyncio.create_task(self._state_loop(), name=f"autochat_state_{self.id}")
        self._planner_task = asyncio.create_task(self._planner_loop(), name=f"autochat_planner_{self.id}")
        self._sender_task = asyncio.create_task(self._sender_loop(), name=f"autochat_sender_{self.id}")

        # Если восстанавливаемся при рестарте и last_any_message_at свежий —
        # всё равно стартуем с in_chat=false. Следующее inbound запустит
        # enter-timer, поведение чуть медленнее но корректное.
        await self._persist_state()

    async def stop(self, *, reason: str = "stopped") -> None:
        """
        Мягкая остановка: ждём завершения текущей отправки (sender_lock),
        отменяем всё остальное.

        Идемпотентна: повторные вызовы — no-op.
        """
        if self._stopped.is_set():
            return
        self._stopped.set()

        # Гасим вспомогательные таймеры
        for task in (self._enter_task, self._idle_task):
            if task and not task.done():
                task.cancel()

        # Будим планнер, чтобы вышел из ожидания bump'а
        self._planner_bump.set()
        self._media_ready.set()

        # Сигнализируем sender'у маркером
        try:
            self._send_queue.put_nowait(None)
        except Exception:
            pass

        # Ждём завершения тасков (без бесконечного ожидания)
        for task in (self._sender_task, self._planner_task, self._state_task):
            if task is None:
                continue
            if not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                log.exception(
                    "autochat session %s: task %s errored on stop",
                    self.id, task.get_name(),
                )

        log.info("autochat session %s: stopped (%s)", self.id, reason)

    def is_running(self) -> bool:
        return not self._stopped.is_set()

    # ─────────────────────────────────────────────────────────────────
    # Вход событий от AutoChatService
    # ─────────────────────────────────────────────────────────────────

    async def handle_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Положить событие в очередь state_loop'а."""
        if self._stopped.is_set():
            return
        await self._queue.put((kind, payload))

    # ─────────────────────────────────────────────────────────────────
    # state_loop — двигает state machine
    # ─────────────────────────────────────────────────────────────────

    async def _state_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    kind, payload = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue

                try:
                    if kind == EV_INBOUND:
                        await self._on_inbound(payload)
                    elif kind == EV_OUTBOUND:
                        await self._on_outbound(payload)
                    elif kind == EV_MEDIA_UPDATED:
                        await self._on_media_updated(payload)
                    elif kind == EV_TYPING:
                        await self._on_typing(payload)
                except Exception:
                    log.exception(
                        "autochat session %s: state_loop handler error (kind=%s)",
                        self.id, kind,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("autochat session %s: state_loop crashed", self.id)

    # ─── Обработчики входящих событий ────────────────────────────────

    async def _on_inbound(self, payload: dict[str, Any]) -> None:
        """
        Inbound сообщение от собеседника (message.saved, is_outgoing=false).
        """
        msg_time = _parse_dt(payload.get("date")) or _now()
        msg_id = payload.get("message_id")
        has_pending = bool(payload.get("_has_pending_media", False))

        # 1. Запомним возраст последнего сообщения ДО этого inbound —
        # нужен для расчёта enter-delay.
        prev_last_any = self.last_any_message_at

        # 2. Обновим timestamps в памяти (и в БД)
        self.last_their_message_at = msg_time
        self.last_any_message_at = msg_time
        await self._persist_state()

        # 3. Если медиа ещё не готово — отметим, что это inbound в ожидании.
        if has_pending and msg_id is not None:
            self._pending_media_msg_ids.add(msg_id)

        # 4. Поведение зависит от InChat
        if not self.in_chat:
            # InChat=0: первый inbound после тишины — запускаем enter_timer.
            # Повторные inbound пока таймер идёт — НЕ перезапускают его
            # (по договорённости в docs/autochat.md).
            if self._enter_task is None or self._enter_task.done():
                age_sec = _age_sec(prev_last_any, msg_time)
                delay_key = _enter_delay_key(age_sec)
                delay = await _get_setting_int(delay_key)
                log.info(
                    "autochat session %s: scheduling enter in %ds (age=%s, key=%s)",
                    self.id, delay, age_sec, delay_key,
                )
                self._enter_task = asyncio.create_task(
                    self._enter_after(delay), name=f"autochat_enter_{self.id}",
                )
        else:
            # InChat=1: живой человек в открытом чате сразу видит сообщение.
            # Помечаем весь диалог прочитанным (read_message без message_id
            # делает ReadHistoryRequest до последнего).
            await self._mark_dialog_read_safe()
            # Тригерим planner + ресетим idle.
            self._bump_planner()
            self._reset_idle()
            # Если у inbound есть pending media — медиа-готовность ещё не
            # наступила. planner сам её потом проверит перед генерацией.

    async def _on_outbound(self, payload: dict[str, Any]) -> None:
        """
        Наше исходящее (is_outgoing=True). Обновляет last_our/last_any.
        На idle-timer работает как новая активность.
        """
        msg_time = _parse_dt(payload.get("date")) or _now()
        self.last_our_activity_at = msg_time
        self.last_any_message_at = msg_time
        await self._persist_state()
        if self.in_chat:
            self._reset_idle()

    async def _on_media_updated(self, payload: dict[str, Any]) -> None:
        """
        message.updated — транскрипция или описание готовы.
        Снимаем блок pending_media для этого message_id если он был.
        """
        msg_id = payload.get("message_id")
        if msg_id is None:
            return
        # Проверим есть ли у этого message ещё pending media
        has_pending = await self._message_has_pending_media(msg_id)
        if not has_pending:
            self._pending_media_msg_ids.discard(msg_id)
            if not self._pending_media_msg_ids:
                self._media_ready.set()
        # В любом случае для InChat=1 тригерим planner: контекст обновился,
        # возможно стоит сгенерить новый ответ.
        if self.in_chat:
            self._bump_planner()

    async def _on_typing(self, payload: dict[str, Any]) -> None:
        """Собеседник печатает → ресетим reply_timer (если InChat=1)."""
        if self.in_chat:
            self._bump_planner()

    # ─── Таймеры ─────────────────────────────────────────────────────

    async def _enter_after(self, delay_sec: int) -> None:
        try:
            await asyncio.sleep(delay_sec)
        except asyncio.CancelledError:
            return

        if self._stopped.is_set() or self.in_chat:
            return

        log.info("autochat session %s: entering chat", self.id)
        self.in_chat = True
        await self._persist_state()

        await self._mark_dialog_read_safe()

        await bus.publish(
            module=Module.AUTOCHAT,
            type=EventType.AUTOCHAT_ENTERED_CHAT,
            status=Status.SUCCESS,
            account_id=self.account_id,
            data={"session_id": self.id, "delay_sec": delay_sec},
        )

        # Запускаем idle-отсчёт и тригерим planner — в чате могут быть
        # непрочитанные сообщения, по которым надо ответить.
        self._reset_idle()
        self._bump_planner()

    def _reset_idle(self) -> None:
        """Перезапустить idle-timer (3 минуты без сообщений → InChat=0)."""
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(
            self._idle_countdown(), name=f"autochat_idle_{self.id}",
        )

    async def _idle_countdown(self) -> None:
        try:
            idle_sec = await _get_setting_int("autochat.idle_leave_sec")
            await asyncio.sleep(idle_sec)
        except asyncio.CancelledError:
            return
        if self._stopped.is_set() or not self.in_chat:
            return
        log.info("autochat session %s: leaving chat (idle)", self.id)
        self.in_chat = False
        await self._persist_state()
        await bus.publish(
            module=Module.AUTOCHAT,
            type=EventType.AUTOCHAT_LEFT_CHAT,
            status=Status.SUCCESS,
            account_id=self.account_id,
            data={"session_id": self.id},
        )

    def _bump_planner(self) -> None:
        self._planner_bump.set()

    # ─────────────────────────────────────────────────────────────────
    # planner_loop — ждёт reply_timer и вызывает LLM
    # ─────────────────────────────────────────────────────────────────

    async def _planner_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                # Ждём первого bump'а — признака что в чате что-то изменилось.
                try:
                    await asyncio.wait_for(self._planner_bump.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                self._planner_bump.clear()

                if self._stopped.is_set():
                    break
                if not self.in_chat:
                    continue

                # reply_timer: 30с тишины. Каждый bump ресетит таймер.
                reply_sec = await _get_setting_int("autochat.reply_timer_sec")
                if not await self._wait_reply_timer(reply_sec):
                    continue  # bump пришёл во время ожидания? уже сбросился

                if self._stopped.is_set() or not self.in_chat:
                    continue

                # Ждём готовность всех pending-медиа у inbound сообщений
                # (максимум 2 минуты — чтобы не зависнуть если медиа-модули лежат).
                if self._pending_media_msg_ids:
                    await self._wait_media_ready(timeout=120)

                # Генерация и постановка сегментов в очередь на отправку.
                try:
                    await self._generate_and_enqueue()
                except AutoSessionExpired:
                    # Сессия TG протухла — выходим и завершаем AutoChat-сессию.
                    await self._fail("session_expired")
                    return
                except Exception as e:
                    log.exception(
                        "autochat session %s: generation failed", self.id,
                    )
                    # Не валим сессию — OpenRouter может ожить. Публикуем
                    # ошибку в шину, ждём следующего триггера.
                    await bus.publish(
                        module=Module.AUTOCHAT,
                        type=EventType.AUTOCHAT_SESSION_ERROR,
                        status=Status.ERROR,
                        account_id=self.account_id,
                        data={
                            "session_id": self.id,
                            "message": "generation_failed",
                            "error": str(e)[:500],
                        },
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("autochat session %s: planner_loop crashed", self.id)

    async def _wait_reply_timer(self, seconds: int) -> bool:
        """
        Ждём `seconds` тишины. Каждый bump (новое inbound/typing/media_upd)
        перезапускает отсчёт. Возвращает True когда таймер истёк,
        False если во время ожидания произошёл stop.
        """
        while True:
            if self._stopped.is_set():
                return False
            try:
                await asyncio.wait_for(self._planner_bump.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                return True
            # Был bump — сбрасываем и ждём заново
            self._planner_bump.clear()
            if self._stopped.is_set():
                return False
            # Небольшая защита от "ложных" bump'ов через media_updated:
            # если нет новых сообщений — продолжаем ждать.
            continue

    async def _wait_media_ready(self, *, timeout: float) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while self._pending_media_msg_ids and not self._stopped.is_set():
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                log.warning(
                    "autochat session %s: waiting_media timeout, generating anyway",
                    self.id,
                )
                return
            self._media_ready.clear()
            try:
                await asyncio.wait_for(self._media_ready.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return

    # ─── Генерация ───────────────────────────────────────────────────

    async def _generate_and_enqueue(self) -> None:
        if self.dialog_id is None:
            log.warning(
                "autochat session %s: dialog_id not set, cannot build context",
                self.id,
            )
            return

        pool = db.get_pool()
        async with pool.acquire() as conn:
            messages = await build_conversation_context(
                conn,
                dialog_id=self.dialog_id,
                system_prompt=self.system_prompt,
                now=_now(),
            )

        req_event = await bus.publish(
            module=Module.AUTOCHAT,
            type=EventType.AUTOCHAT_GENERATION_REQUESTED,
            status=Status.IN_PROGRESS,
            account_id=self.account_id,
            data={
                "session_id": self.id,
                "messages_count": len(messages),
            },
        )
        parent_id = req_event["id"]

        retries = await _get_setting_int("autochat.openrouter_retries")
        response = await _call_llm_with_retries(messages, retries=retries)

        segments = parse_segments(response)
        log.info(
            "autochat session %s: LLM answered %d segments", self.id, len(segments),
        )

        await bus.publish(
            module=Module.AUTOCHAT,
            type=EventType.AUTOCHAT_GENERATION_DONE,
            status=Status.SUCCESS if segments else Status.ERROR,
            parent_id=parent_id,
            account_id=self.account_id,
            data={
                "session_id": self.id,
                "segments_count": len(segments),
            },
        )

        total = len(segments)
        for i, seg in enumerate(segments, 1):
            await self._send_queue.put({"index": i, "total": total, "text": seg, "parent_id": parent_id})

    # ─────────────────────────────────────────────────────────────────
    # sender_loop — отправка сегментов с имитацией печати
    # ─────────────────────────────────────────────────────────────────

    async def _sender_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                item = await self._send_queue.get()
                if item is None:  # маркер остановки
                    return
                try:
                    await self._send_segment(item)
                except AutoSessionExpired:
                    await self._fail("session_expired")
                    return
                except Exception as e:
                    log.exception(
                        "autochat session %s: send_segment failed", self.id,
                    )
                    await bus.publish(
                        module=Module.AUTOCHAT,
                        type=EventType.AUTOCHAT_SESSION_ERROR,
                        status=Status.ERROR,
                        account_id=self.account_id,
                        data={
                            "session_id": self.id,
                            "message": "send_failed",
                            "error": str(e)[:500],
                        },
                    )
                    # Попытка отправить сегмент провалилась — считаем сессию
                    # завалившейся. Скорее всего нас заблокировали.
                    await self._fail("send_failed")
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("autochat session %s: sender_loop crashed", self.id)

    async def _send_segment(self, item: dict[str, Any]) -> None:
        text: str = item["text"]
        index: int = item["index"]
        total: int = item["total"]
        parent_id = item.get("parent_id")

        wrapper = self._get_wrapper(self.account_id)
        if wrapper is None:
            raise RuntimeError("worker not running")

        # Имитация печати
        ms_per_char = await _get_setting_int("autochat.typing_ms_per_char")
        typing_sec = max(0.6, min(8.0, len(text) * ms_per_char / 1000 + random.uniform(0.3, 0.9)))

        try:
            await wrapper.set_typing(self.telegram_user_id)
        except Exception:
            log.debug("autochat session %s: set_typing failed (non-fatal)", self.id)

        await asyncio.sleep(typing_sec)

        try:
            await wrapper.cancel_typing(self.telegram_user_id)
        except Exception:
            pass

        # Отправка
        sent = await wrapper.send_message(self.telegram_user_id, text)
        tg_msg_id = sent.get("telegram_message_id")

        # Публикуем message.received для history — Telethon сам не триггерит
        # NewMessage handler для собственных отправок (см. STAGE_4_REPORT).
        if tg_msg_id is not None:
            await bus.publish(
                module=Module.WRAPPER,
                type=EventType.MESSAGE_RECEIVED,
                status=Status.SUCCESS,
                account_id=self.account_id,
                data={
                    "telegram_message_id": tg_msg_id,
                    "telegram_user_id": self.telegram_user_id,
                    "is_outgoing": True,
                    "date": sent.get("date") or bus.now_utc(),
                    "text": text,
                    "reply_to_telegram_message_id": None,
                    "forward_from": None,
                    "media_group_id": None,
                    "peer_profile": None,
                    "media": [],
                },
            )

        # Обновляем свой state
        self.last_our_activity_at = _now()
        self.last_any_message_at = self.last_our_activity_at
        await self._persist_state()
        if self.in_chat:
            self._reset_idle()

        await bus.publish(
            module=Module.AUTOCHAT,
            type=EventType.AUTOCHAT_SEGMENT_SENT,
            status=Status.SUCCESS,
            parent_id=parent_id,
            account_id=self.account_id,
            data={
                "session_id": self.id,
                "segment_index": index,
                "segments_total": total,
                "telegram_message_id": tg_msg_id,
            },
        )

        # Пауза между сегментами 1–3 сек (кроме последнего)
        if index < total:
            await asyncio.sleep(random.uniform(1.0, 3.0))

    # ─────────────────────────────────────────────────────────────────
    # Сервисные запросы к БД
    # ─────────────────────────────────────────────────────────────────

    async def _persist_state(self) -> None:
        """Синхронизировать оперативные поля state в БД."""
        pool = db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE autochat_sessions SET
                    in_chat = $2,
                    last_our_activity_at = $3,
                    last_their_message_at = $4,
                    last_any_message_at = $5,
                    updated_at = NOW()
                WHERE id = $1
                """,
                self.id,
                self.in_chat,
                self.last_our_activity_at,
                self.last_their_message_at,
                self.last_any_message_at,
            )

    async def set_dialog_id(self, dialog_id: int) -> None:
        """Проставить dialog_id после того как history создал запись."""
        if self.dialog_id == dialog_id:
            return
        self.dialog_id = dialog_id
        pool = db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE autochat_sessions SET dialog_id = $2, updated_at = NOW() WHERE id = $1",
                self.id, dialog_id,
            )

    async def _message_has_pending_media(self, message_id: int) -> bool:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1 FROM media
                WHERE message_id = $1
                  AND (transcription_status = 'pending' OR description_status = 'pending')
                LIMIT 1
                """,
                message_id,
            )
        return row is not None

    async def _mark_dialog_read_safe(self) -> None:
        """Пометить диалог прочитанным. Ошибки — best-effort."""
        try:
            wrapper = self._get_wrapper(self.account_id)
            if wrapper is None:
                return
            await wrapper.read_message(self.telegram_user_id)
        except Exception:
            log.debug(
                "autochat session %s: mark_read failed (non-fatal)", self.id,
            )

    # ─── Фатальные ошибки ────────────────────────────────────────────

    async def _fail(self, reason: str) -> None:
        """Перевести сессию в status=failed и опубликовать событие."""
        log.warning("autochat session %s: failing (%s)", self.id, reason)
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
                    self.id, reason,
                )
        except Exception:
            log.exception("autochat session %s: persist fail status errored", self.id)

        try:
            await bus.publish(
                module=Module.AUTOCHAT,
                type=EventType.AUTOCHAT_SESSION_ERROR,
                status=Status.ERROR,
                account_id=self.account_id,
                data={"session_id": self.id, "message": reason},
            )
        except Exception:
            log.exception("autochat session %s: publish fail errored", self.id)

        # Таска остановки — не ждём сами себя, гасим в фоне
        asyncio.create_task(self.stop(reason=reason))


# ─────────────────────────────────────────────────────────────────────
# Хелперы
# ─────────────────────────────────────────────────────────────────────

def _parse_dt(value: Any) -> datetime | None:
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


def _age_sec(previous_any: datetime | None, now_like: datetime) -> int | None:
    if previous_any is None:
        return None
    try:
        delta = (now_like - previous_any).total_seconds()
    except Exception:
        return None
    return max(0, int(delta))


def _enter_delay_key(age_sec: int | None) -> str:
    """Какую группу задержки брать в зависимости от возраста."""
    if age_sec is None or age_sec >= 10 * 60:
        return "autochat.enter_delay_long_sec"
    if age_sec >= 5 * 60:
        return "autochat.enter_delay_mid_sec"
    return "autochat.enter_delay_short_sec"


# ─────────────────────────────────────────────────────────────────────
# LLM-вызов с ретраями
# ─────────────────────────────────────────────────────────────────────

async def _call_llm_with_retries(
    messages: list[dict[str, Any]],
    *,
    retries: int,
) -> str:
    """
    chat_completion с ретраями. Ошибки OpenRouter — до retries повторов,
    потом бросаем последнюю как OpenRouterError.
    """
    attempts = max(1, 1 + retries)
    last_error: Exception | None = None

    for attempt in range(attempts):
        try:
            text = await chat_completion(messages)
            return text
        except OpenRouterError as e:
            last_error = e
            log.warning(
                "autochat: OpenRouter error attempt=%d/%d: %s",
                attempt + 1, attempts, e,
            )
            # короткая пауза перед следующей попыткой
            await asyncio.sleep(min(2 ** attempt, 10))
            continue

    # Все попытки исчерпаны
    raise last_error or OpenRouterError("chat_completion failed after retries")
