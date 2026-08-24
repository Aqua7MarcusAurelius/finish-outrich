"""Слушатель шины — пушит финальный статус в партнёрскую CRM.

Триггер: событие `dialog.status_changed`. Если новый пузырь
терминальный (у `dialog_statuses.partner_terminal_status` есть код)
И у диалога `from_crm=TRUE` — отправляем `POST /leads/:u/status` в CRM.

Какой именно код отправляем:
1. Сначала смотрим `dialogs.partner_crm_terminal_reason` — там лежит
   "что хотим послать" (записывается оценщиком или silence_watcher,
   точная причина — `ready_for_transfer` / `invalid` / `no_response`).
2. Если NULL (например оператор поставил пузырь руками) — берём из
   `dialog_statuses.partner_terminal_status` напрямую.

Защита от дублей: после успешного push'а очищаем `partner_crm_terminal_reason`
обратно в NULL. Если кто-то снова поставит тот же пузырь — повторно
не будем дёргать CRM (defensive — CRM сама умеет идемпотентно, но
лишних запросов не делаем).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from core import bus, db
from core.events import EventType
from .errors import PartnerCrmError
from .service import PartnerCrmService

log = logging.getLogger(__name__)

FINALIZER_GROUP = "partner-crm-finalizer"
FINALIZER_CONSUMER = "partner-crm-finalizer-1"


class PartnerCrmFinalizer:
    """
    Bus consumer для terminal-статуса. Создаётся в lifespan, владеет
    одной asyncio.Task.
    """

    def __init__(self, service: PartnerCrmService) -> None:
        self._service = service
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        await bus.ensure_group(FINALIZER_GROUP)
        log.info("PartnerCrmFinalizer loop started")

        while not self._stop_event.is_set():
            try:
                batch = await bus.read_group(
                    FINALIZER_GROUP, FINALIZER_CONSUMER,
                    count=20, block_ms=5000,
                )
                if not batch:
                    continue
                ack_ids: list[str] = []
                for stream_id, event in batch:
                    try:
                        await self._handle(event)
                        ack_ids.append(stream_id)
                        await bus.record_success(FINALIZER_GROUP, stream_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.exception(
                            "partner_crm finalizer: failed event %s",
                            event.get("id"),
                        )
                        force_ack = await bus.record_failure(
                            FINALIZER_GROUP, stream_id, event, e,
                        )
                        if force_ack:
                            ack_ids.append(stream_id)
                if ack_ids:
                    await bus.ack_group(FINALIZER_GROUP, ack_ids)
            except asyncio.CancelledError:
                log.info("PartnerCrmFinalizer cancelled")
                raise
            except Exception:
                log.exception("PartnerCrmFinalizer loop error")
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stop_event.set()

    # ─── Dispatcher ──────────────────────────────────────────────────

    async def _handle(self, event: dict[str, Any]) -> None:
        if event.get("type") != EventType.DIALOG_STATUS_CHANGED:
            return
        data = event.get("data") or {}
        dialog_id = data.get("dialog_id")
        status_id = data.get("status_id")
        if dialog_id is None or status_id is None:
            return

        if not self._service.is_configured():
            return  # CRM-настройки пустые — молча скип

        # Подтянем dialog + status в одном запросе.
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT d.id           AS dialog_id,
                       d.account_id   AS account_id,
                       d.username     AS username,
                       d.from_crm     AS from_crm,
                       d.partner_crm_terminal_reason AS stored_reason,
                       ds.partner_terminal_status AS pubble_terminal
                FROM dialogs d
                LEFT JOIN dialog_statuses ds ON ds.id = d.status_id
                WHERE d.id = $1
                """,
                int(dialog_id),
            )

        if row is None:
            log.warning(
                "partner_crm finalizer: dialog %s not found, skip", dialog_id,
            )
            return

        if not bool(row["from_crm"]):
            return  # ручной лид — в CRM ничего не пушим

        # Если у пузыря нет partner_terminal_status — это промежуточный.
        # Промежуточные статусы в CRM не отправляем (CRM остаётся
        # in_progress всё время работы с лидом).
        pubble_terminal = row["pubble_terminal"]
        if not pubble_terminal:
            return

        username = row["username"]
        if not username:
            log.warning(
                "partner_crm finalizer: dialog %s without username (from_crm), "
                "cannot push status — skip", dialog_id,
            )
            return

        # 2026-08-23: успешные диалоги проходят модерацию в TG-группе
        # (modules/report_bot). Она сама пушит статус и ПОСЛЕ этого
        # ставит терминальный пузырь — событие о котором прилетает сюда.
        # Без этой проверки мы бы отправили партнёру второй статус,
        # затерев решение модератора (`transferred` → `ready_for_transfer`).
        async with pool.acquire() as conn:
            moderated = await conn.fetchval(
                "SELECT pushed_status FROM report_moderation "
                "WHERE dialog_id = $1 AND pushed_at IS NOT NULL",
                int(dialog_id),
            )
        if moderated is not None:
            log.debug(
                "partner_crm finalizer: диалог %s уже финализирован "
                "модерацией (%s) — пропуск", dialog_id, moderated,
            )
            return

        # Приоритет: stored_reason из dialogs (точная причина закрытия).
        # Если NULL (оператор сам нажал терминальный пузырь) — берём
        # маппинг пузыря.
        status_to_push = row["stored_reason"] or pubble_terminal

        try:
            await self._service.push_lead_finalized(
                account_id=int(row["account_id"]),
                username=username,
                terminal_status=status_to_push,
            )
        except PartnerCrmError as e:
            log.warning(
                "partner_crm finalizer: push failed dialog=%s username=%s "
                "status=%s err=%s",
                dialog_id, username, status_to_push, e,
            )
            return

        # Push прошёл — стираем stored_reason чтобы повторное событие
        # dialog.status_changed на тот же terminal не вызвало второй push.
        # Сам пузырь остаётся, защита от регресса работает на уровне UI.
        if row["stored_reason"] is not None:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE dialogs SET partner_crm_terminal_reason = NULL,
                                       updated_at = NOW()
                    WHERE id = $1
                    """,
                    dialog_id,
                )
