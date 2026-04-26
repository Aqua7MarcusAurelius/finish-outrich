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
    extract_finish_marker,
    parse_segments,
)
from .prompts import load_for_account, render_reply_system

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
        on_finished_by_llm: Callable[[int], Any] | None = None,
    ):
        # Основные поля из БД
        self.id: int = row["id"]
        self.account_id: int = row["account_id"]
        self.dialog_id: int | None = row.get("dialog_id")
        self.telegram_user_id: int = row["telegram_user_id"]
        self.target_username: str = row["target_username"]
        self.system_prompt: str = row["system_prompt"]

        self._get_wrapper = get_wrapper
        # Callback в AutoChatService — вызывается когда LLM поставила
        # <finishdialog/> и последний сегмент уже отправлен. Сервис
        # делает stop_session и чистит свои индексы.
        self._on_finished_by_llm = on_finished_by_llm

        # Текущее состояние (синхронизируется с БД на ключевых точках)
        self.in_chat: bool = bool(row.get("in_chat", False))
        self.last_our_activity_at: datetime | None = row.get("last_our_activity_at")
        self.last_their_message_at: datetime | None = row.get("last_their_message_at")
        self.last_any_message_at: datetime | None = row.get("last_any_message_at")

        # Внутренние очереди / флаги
        self._queue: asyncio.Queue = asyncio.Queue()
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._planner_bump: asyncio.Event = asyncio.Event()

        # Запущенные таски (заполняются в start())
        self._state_task: asyncio.Task | None = None
        self._planner_task: asyncio.Task | None = None
        self._sender_task: asyncio.Task | None = None

        # Вспомогательные таймеры
        self._enter_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None

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

        # 1. Запомним возраст последнего сообщения ДО этого inbound —
        # нужен для расчёта enter-delay.
        prev_last_any = self.last_any_message_at

        # 2. Обновим timestamps в памяти (и в БД)
        self.last_their_message_at = msg_time
        self.last_any_message_at = msg_time
        await self._persist_state()

        # 3. Поведение зависит от InChat
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
        Сам по себе не влияет на таймеры: ожидание готовности медиа —
        через polling БД перед генерацией, см. _wait_for_transcriptions().
        """
        # no-op: оставлено для совместимости с dispatcher'ом.
        return

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

                # Перед генерацией — дождаться готовности всех транскрипций/
                # описаний по всему диалогу (polling 30с, max 10 попыток).
                await self._wait_for_transcriptions()

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

    async def _wait_for_transcriptions(
        self, *, poll_sec: int = 30, max_attempts: int = 10,
    ) -> None:
        """
        Перед генерацией LLM-ответа: проверяем нет ли в истории диалога
        сообщений с незавершённой транскрипцией или описанием. Если есть —
        откладываем генерацию на `poll_sec` и проверяем снова. Повторяем
        до `max_attempts` раз (дефолт = 10 × 30с = 5 минут).

        После max_attempts всё равно идём генерировать — в контексте у LLM
        будут пометки вида `[голос: расшифровывается…]` и она ответит
        общими словами.
        """
        for attempt in range(1, max_attempts + 1):
            if self._stopped.is_set():
                return
            if not await self._dialog_has_pending_media():
                return
            log.info(
                "autochat session %s: pending media in dialog, "
                "postponing generation %ds (attempt %d/%d)",
                self.id, poll_sec, attempt, max_attempts,
            )
            try:
                await asyncio.sleep(poll_sec)
            except asyncio.CancelledError:
                return
        log.warning(
            "autochat session %s: transcription not ready after %d attempts, "
            "generating anyway",
            self.id, max_attempts,
        )

    async def _dialog_has_pending_media(self) -> bool:
        """Есть ли в текущем диалоге media с pending статусом?"""
        if self.dialog_id is None:
            return False
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1 FROM media m
                JOIN messages msg ON msg.id = m.message_id
                WHERE msg.dialog_id = $1
                  AND msg.deleted_at IS NULL
                  AND (m.transcription_status = 'pending'
                       OR m.description_status = 'pending')
                LIMIT 1
                """,
                self.dialog_id,
            )
        return row is not None

    # ─── Генерация ───────────────────────────────────────────────────

    async def _generate_and_enqueue(self) -> None:
        if self.dialog_id is None:
            log.warning(
                "autochat session %s: dialog_id not set, cannot build context",
                self.id,
            )
            return

        # Гейт по per-worker промту: если все 8 reply-секций пустые —
        # автоответ не генерируем. Сессия остаётся живой; следующий bump
        # снова попробует, и как только оператор заполнит хоть одно поле
        # в редакторе — ответы пойдут.
        worker_prompts = await load_for_account(self.account_id)
        if not worker_prompts.has_any_reply_field():
            log.info(
                "autochat session %s: skip generation, all reply prompt fields empty (account %s)",
                self.id, self.account_id,
            )
            await bus.publish(
                module=Module.AUTOCHAT,
                type=EventType.AUTOCHAT_GENERATION_SKIPPED,
                status=Status.ERROR,
                account_id=self.account_id,
                data={
                    "session_id": self.id,
                    "reason": "no_prompt",
                    "message": "У воркера все поля reply-промта пустые — заполни хотя бы одно в редакторе.",
                },
            )
            return

        rendered_template = render_reply_system(worker_prompts)

        pool = db.get_pool()
        async with pool.acquire() as conn:
            messages = await build_conversation_context(
                conn,
                dialog_id=self.dialog_id,
                system_prompt=self.system_prompt,
                now=_now(),
                prompt_override=rendered_template,
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

        # <finishdialog/> вырезаем ДО parse_segments — иначе маркер
        # может попасть в конец последнего <msg> или сесть отдельным
        # сегментом и улететь в Telegram.
        response_clean, finish_requested = extract_finish_marker(response)
        segments = parse_segments(response_clean)
        log.info(
            "autochat session %s: LLM answered %d segments (finish=%s)",
            self.id, len(segments), finish_requested,
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
                "finish_requested": finish_requested,
            },
        )

        total = len(segments)
        for i, seg in enumerate(segments, 1):
            item = {"index": i, "total": total, "text": seg, "parent_id": parent_id}
            # Финиш триггерим после отправки ПОСЛЕДНЕГО сегмента — чтобы
            # прощальный реплай уехал, а потом сессия погасла.
            if finish_requested and i == total:
                item["finish_after"] = True
            await self._send_queue.put(item)

        # Edge: LLM прислала только маркер без сегментов. Гасим сразу.
        if finish_requested and total == 0:
            log.info(
                "autochat session %s: finish requested but no segments — stopping immediately",
                self.id,
            )
            await self._publish_finished_by_llm(parent_id)
            if self._on_finished_by_llm is not None:
                asyncio.create_task(self._on_finished_by_llm(self.id))

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

        # Финиш по сигналу LLM — после отправки последнего сегмента.
        # Делаем в фоновой таске, чтобы sender_loop успел вернуться из
        # _send_segment до того как service.stop_session начнёт его
        # отменять (иначе будет ждать самого себя).
        if item.get("finish_after"):
            await self._publish_finished_by_llm(parent_id)
            if self._on_finished_by_llm is not None:
                asyncio.create_task(self._on_finished_by_llm(self.id))

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

    # ─── Финиш по сигналу LLM ────────────────────────────────────────

    async def _publish_finished_by_llm(self, parent_id: str | None) -> None:
        """Публикует событие autochat.finished_by_llm. Сам stop делает callback."""
        try:
            await bus.publish(
                module=Module.AUTOCHAT,
                type=EventType.AUTOCHAT_FINISHED_BY_LLM,
                status=Status.SUCCESS,
                parent_id=parent_id,
                account_id=self.account_id,
                data={"session_id": self.id},
            )
        except Exception:
            log.exception(
                "autochat session %s: publish finished_by_llm failed", self.id,
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
