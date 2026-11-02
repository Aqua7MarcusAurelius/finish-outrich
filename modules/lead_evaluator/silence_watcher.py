"""Сторож тишины — отслеживает диалоги, в которых пропала активность.

Раз в `lead_evaluator.silence_check_interval_sec` секунд:

- `Общение` + последнее **входящее** старше `silence_to_obryv_hours`
  (дефолт 4 ч) → переводим в `Обрыв`.

- `Первый контакт` + последнее сообщение (любое) старше
  `silence_to_close_hours` (дефолт 48 ч) → терминальный `Завершен провал`
  с `partner_crm_terminal_reason='no_response'`.

- `Обрыв` + последнее сообщение (любое) старше `silence_to_close_hours` →
  тот же терминальный `Завершен провал` + `no_response`.

Кандидаты повторно проверяются под блокировкой строки диалога. Статус,
reason и происхождение меняются одной транзакцией; событие публикуется
после commit. Незавершённые ответы и решения оценщика/модератора защищены.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from core import bus, db
from modules.dialog_statuses import service as dialog_statuses_service

log = logging.getLogger(__name__)

DEFAULT_CHECK_INTERVAL_SEC = 300   # 5 минут
DEFAULT_TO_OBRYV_HOURS = 4
DEFAULT_TO_CLOSE_HOURS = 48


async def _get_int_setting(key: str, default: int) -> int:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT value FROM settings WHERE key=$1", key,
        )
    try:
        return int(val) if val is not None else default
    except Exception:
        return default


async def _load_status_id_by_label(label: str) -> int | None:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT id FROM dialog_statuses WHERE label = $1 LIMIT 1",
            label,
        )


class SilenceWatcher:
    """
    Создаётся в lifespan после LeadEvaluatorService (логически независим,
    но удобнее держать рядом). Не имеет внутреннего состояния — каждый
    тик читает свежее.
    """

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        log.info("SilenceWatcher loop started")
        while not self._stop_event.is_set():
            try:
                interval = await _get_int_setting(
                    "lead_evaluator.silence_check_interval_sec",
                    DEFAULT_CHECK_INTERVAL_SEC,
                )
                await self._tick()
            except asyncio.CancelledError:
                log.info("SilenceWatcher cancelled")
                raise
            except Exception:
                log.exception("SilenceWatcher tick error")
                interval = DEFAULT_CHECK_INTERVAL_SEC

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=max(10, interval),
                )
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop_event.set()

    # ─── Один тик ────────────────────────────────────────────────────

    async def _tick(self) -> None:
        # ID пузырей берём свежими — оператор мог что-то перетасовать.
        s_pervyi = await _load_status_id_by_label("Первый контакт")
        s_obshenie = await _load_status_id_by_label("Общение")
        s_obryv = await _load_status_id_by_label("Обрыв")
        s_zavershen_proval = await _load_status_id_by_label("Завершен провал")

        if None in (s_pervyi, s_obshenie, s_obryv, s_zavershen_proval):
            log.warning(
                "SilenceWatcher: один из системных пузырей не найден в каталоге "
                "(переименован?) — пропускаем тик. "
                "ids: pervyi=%s obshenie=%s obryv=%s proval=%s",
                s_pervyi, s_obshenie, s_obryv, s_zavershen_proval,
            )
            return

        obryv_hours = await _get_int_setting(
            "lead_evaluator.silence_to_obryv_hours", DEFAULT_TO_OBRYV_HOURS,
        )
        close_hours = await _get_int_setting(
            "lead_evaluator.silence_to_close_hours", DEFAULT_TO_CLOSE_HOURS,
        )

        await self._transition_obshenie_to_obryv(
            s_obshenie, s_obryv, obryv_hours,
        )
        await self._close_silent_dialogs(
            s_pervyi, s_obryv, s_zavershen_proval, close_hours,
        )

    # ─── Общение → Обрыв ─────────────────────────────────────────────

    async def _transition_obshenie_to_obryv(
        self, s_obshenie: int, s_obryv: int, hours: int,
    ) -> None:
        """
        Диалоги в "Общение" у которых последнее ВХОДЯЩЕЕ старше N часов —
        переводим в "Обрыв". Считаем по `messages.is_outgoing=false`.
        """
        pool = db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.id AS dialog_id
                FROM dialogs d
                LEFT JOIN LATERAL (
                    SELECT MAX(date) AS last_in
                    FROM messages
                    WHERE dialog_id = d.id AND is_outgoing = FALSE AND deleted_at IS NULL
                ) m ON TRUE
                WHERE d.status_id = $1
                  AND (m.last_in IS NULL
                       OR m.last_in < NOW() - ($2::int || ' hours')::interval)
                """,
                s_obshenie, int(hours),
            )

        for r in rows:
            await self._try_transition(
                int(r["dialog_id"]), [s_obshenie], s_obryv, hours,
                incoming_only=True, source="event:silence_timer.obryv",
            )

    # ─── Первый контакт / Обрыв → Завершен провал ────────────────────

    async def _close_silent_dialogs(
        self,
        s_pervyi: int,
        s_obryv: int,
        s_proval: int,
        hours: int,
    ) -> None:
        """
        Диалоги в "Первый контакт" или "Обрыв" с любым последним
        сообщением старше N часов — закрываем в "Завершен провал"
        с reason='no_response'.
        """
        pool = db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.id AS dialog_id
                FROM dialogs d
                LEFT JOIN LATERAL (
                    SELECT MAX(date) AS last_any
                    FROM messages WHERE dialog_id = d.id AND deleted_at IS NULL
                ) m ON TRUE
                WHERE d.status_id = ANY($1::int[])
                  AND (m.last_any IS NOT NULL
                       AND m.last_any < NOW() - ($2::int || ' hours')::interval)
                """,
                [s_pervyi, s_obryv], int(hours),
            )

        for r in rows:
            await self._try_transition(
                int(r["dialog_id"]), [s_pervyi, s_obryv], s_proval, hours,
                incoming_only=False, source="event:silence_timer.no_response",
            )

    async def _try_transition(
        self, dialog_id: int, expected_status_ids: list[int], target_status_id: int,
        hours: int, *, incoming_only: bool, source: str,
    ) -> bool:
        """Recheck under the history/status row lock; never close pending work.

        The initial scan is only a candidate list. It cannot authorize a later
        update after history, the evaluator or the operator changed the dialog.
        """
        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                dialog = await conn.fetchrow(
                    "SELECT id, account_id, status_id, partner_crm_terminal_reason "
                    "FROM dialogs WHERE id=$1 FOR UPDATE", dialog_id,
                )
                if (dialog is None or dialog["status_id"] not in expected_status_ids
                        or dialog["partner_crm_terminal_reason"] is not None):
                    return False
                protected = await conn.fetchval(
                    """SELECT
                        EXISTS(SELECT 1 FROM dialog_statuses WHERE id=$2 AND closes_dialog)
                        OR EXISTS(SELECT 1 FROM lead_evaluations WHERE dialog_id=$1)
                        OR EXISTS(SELECT 1 FROM report_moderation WHERE dialog_id=$1)
                        OR EXISTS(SELECT 1 FROM autochat_reply_batches WHERE dialog_id=$1
                          AND phase IN ('generating','ready','sending','finishing','review'))""",
                    dialog_id, dialog["status_id"],
                )
                if protected:
                    return False
                latest = await conn.fetchrow(
                    """SELECT id, date, saved_at, is_outgoing FROM messages
                       WHERE dialog_id=$1 AND deleted_at IS NULL
                       ORDER BY date DESC, telegram_message_id DESC, id DESC LIMIT 1""",
                    dialog_id,
                )
                if latest is None:
                    return False
                if not latest["is_outgoing"]:
                    active = await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM autochat_sessions "
                        "WHERE dialog_id=$1 AND status='active')", dialog_id,
                    )
                    if active:
                        # This is our unprocessed input, not the lead's silence.
                        return False
                activity = latest
                if incoming_only:
                    activity = await conn.fetchrow(
                        """SELECT date, saved_at FROM messages
                           WHERE dialog_id=$1 AND NOT is_outgoing AND deleted_at IS NULL
                           ORDER BY date DESC, telegram_message_id DESC, id DESC LIMIT 1""",
                        dialog_id,
                    ) or latest
                last_activity = max(activity["date"], activity["saved_at"] or activity["date"])
                # Catch-up receives its own grace; an old Telegram timestamp
                # must not immediately close freshly discovered conversation.
                if last_activity >= bus.now_utc() - timedelta(hours=max(1, hours)):
                    return False
                await conn.execute(
                    """UPDATE dialogs SET status_id=$2, status_source=$3,
                           status_source_message_id=$4,
                           partner_crm_terminal_reason=$5, updated_at=NOW()
                       WHERE id=$1""",
                    dialog_id, target_status_id, source, latest["id"],
                    None if incoming_only else "no_response",
                )
        await dialog_statuses_service.publish_status_changed(
            dialog_id, int(dialog["account_id"]), target_status_id, source,
        )
        log.info("SilenceWatcher: dialog=%s status=%s source=%s", dialog_id, target_status_id, source)
        return True
