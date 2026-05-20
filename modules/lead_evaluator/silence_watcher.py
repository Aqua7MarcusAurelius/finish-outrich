"""Сторож тишины — отслеживает диалоги, в которых пропала активность.

Раз в `lead_evaluator.silence_check_interval_sec` секунд:

- `Общение` + последнее **входящее** старше `silence_to_obryv_hours`
  (дефолт 4 ч) → переводим в `Обрыв`.

- `Первый контакт` + последнее сообщение (любое) старше
  `silence_to_close_hours` (дефолт 48 ч) → терминальный `Завершен провал`
  с `partner_crm_terminal_reason='no_response'`.

- `Обрыв` + последнее сообщение (любое) старше `silence_to_close_hours` →
  тот же терминальный `Завершен провал` + `no_response`.

При финализации записываем reason в `dialogs.partner_crm_terminal_reason`
ДО постановки пузыря — чтобы partner_crm (10.8) сразу увидел правильный
код когда среагирует на `dialog.status_changed`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

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
                    WHERE dialog_id = d.id AND is_outgoing = FALSE
                ) m ON TRUE
                WHERE d.status_id = $1
                  AND (m.last_in IS NULL
                       OR m.last_in < NOW() - ($2::int || ' hours')::interval)
                """,
                s_obshenie, int(hours),
            )

        for r in rows:
            dialog_id = int(r["dialog_id"])
            try:
                await dialog_statuses_service.set_dialog_status(
                    dialog_id, s_obryv, source="event:silence_timer.obryv",
                )
                log.info(
                    "SilenceWatcher: dialog %s Общение→Обрыв (тишина >%dч)",
                    dialog_id, hours,
                )
            except dialog_statuses_service.DialogStatusError:
                log.exception(
                    "SilenceWatcher: failed to set Обрыв on dialog %s", dialog_id,
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
                    FROM messages WHERE dialog_id = d.id
                ) m ON TRUE
                WHERE d.status_id = ANY($1::int[])
                  AND (m.last_any IS NOT NULL
                       AND m.last_any < NOW() - ($2::int || ' hours')::interval)
                """,
                [s_pervyi, s_obryv], int(hours),
            )

            # Сначала пишем reason, потом меняем статус — partner_crm в 10.8
            # читает обе колонки и должен видеть согласованное состояние.
            for r in rows:
                dialog_id = int(r["dialog_id"])
                await conn.execute(
                    """
                    UPDATE dialogs
                    SET partner_crm_terminal_reason = 'no_response',
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    dialog_id,
                )

        for r in rows:
            dialog_id = int(r["dialog_id"])
            try:
                await dialog_statuses_service.set_dialog_status(
                    dialog_id, s_proval,
                    source="event:silence_timer.no_response",
                )
                log.info(
                    "SilenceWatcher: dialog %s → Завершен провал "
                    "(тишина >%dч, reason=no_response)",
                    dialog_id, hours,
                )
            except dialog_statuses_service.DialogStatusError:
                log.exception(
                    "SilenceWatcher: failed to close dialog %s as no_response",
                    dialog_id,
                )
