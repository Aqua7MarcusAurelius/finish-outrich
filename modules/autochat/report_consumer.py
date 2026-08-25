"""Триггер генерации HTML-отчёта на терминальный пузырь.

Раньше отчёт строился в `AutoChatService._on_session_finished_by_llm` —
сразу после `<finishdialog/>`. Это работало пока не появился оценщик:
ему нужно время дать вердикт (LLM-вызов), а отчёт уже сохранён без
блока "Оценка лида".

Сейчас флоу другой:
- LLM ставит `<finishdialog/>`         → autochat.finished_by_llm
- lead_evaluator анализирует           → ставит терминальный пузырь
- silence_watcher тоже может закрыть   → ставит терминальный пузырь
- оператор сам кликнул пузырь          → ставит терминальный пузырь
                                        │
                                        ▼ event dialog.status_changed
                                  ReportConsumer ловит → save_report_for_dialog

Так отчёт строится в нужный момент и видит уже заполненный
`lead_evaluations` (если оценщик отработал).

Папка в MinIO выбирается внутри `save_report_for_dialog` по вердикту:
success/ | failed/ | archive/.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from core import bus, db
from core.events import EventType

log = logging.getLogger(__name__)

REPORT_GROUP = "autochat-report-builder"
REPORT_CONSUMER = "autochat-report-builder-1"


async def _is_terminal_status(status_id: int | None) -> bool:
    if status_id is None:
        return False
    pool = db.get_pool()
    async with pool.acquire() as conn:
        closes = await conn.fetchval(
            """
            SELECT closes_dialog
            FROM dialog_statuses WHERE id = $1
            """,
            int(status_id),
        )
    # closes_dialog, а НЕ partner_terminal_status: «Модерация» закрывает
    # диалог (отчёт пора строить), но кода для CRM у неё нет — статус
    # туда отправит модератор кнопкой. См. миграцию 0026.
    return bool(closes)


class ReportConsumer:
    """
    Bus consumer: на dialog.status_changed с терминальным пузырём —
    зовёт save_report_for_dialog.
    """

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        await bus.ensure_group(REPORT_GROUP)
        log.info("ReportConsumer loop started")

        while not self._stop_event.is_set():
            try:
                batch = await bus.read_group(
                    REPORT_GROUP, REPORT_CONSUMER,
                    count=20, block_ms=5000,
                )
                if not batch:
                    continue
                ack_ids: list[str] = []
                for stream_id, event in batch:
                    try:
                        await self._handle(event)
                        ack_ids.append(stream_id)
                        await bus.record_success(REPORT_GROUP, stream_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.exception(
                            "ReportConsumer: failed event %s", event.get("id"),
                        )
                        force_ack = await bus.record_failure(
                            REPORT_GROUP, stream_id, event, e,
                        )
                        if force_ack:
                            ack_ids.append(stream_id)
                if ack_ids:
                    await bus.ack_group(REPORT_GROUP, ack_ids)
            except asyncio.CancelledError:
                log.info("ReportConsumer cancelled")
                raise
            except Exception:
                log.exception("ReportConsumer loop error")
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stop_event.set()

    async def _handle(self, event: dict[str, Any]) -> None:
        if event.get("type") != EventType.DIALOG_STATUS_CHANGED:
            return
        data = event.get("data") or {}
        dialog_id = data.get("dialog_id")
        status_id = data.get("status_id")
        if dialog_id is None:
            return
        if not await _is_terminal_status(status_id):
            return

        # Импорт здесь — чтобы не тащить вес report.py при импорте этого
        # модуля (он подгружает MinIO/SQL зависимости).
        from .report import save_report_for_dialog
        await save_report_for_dialog(int(dialog_id))
