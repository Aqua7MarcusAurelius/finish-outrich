"""Фоновый сторож пула партнёрских лидов.

Раз в `partner_crm.poll_interval_sec` секунд:
1. Считает размер `crm_lead_pool`.
2. Смотрит есть ли хотя бы один **активный** воркер с
   `accounts.lead_source = 'partner_crm'` (т.е. is_running=true в
   worker_auto_runners И lead_source partner_crm).
3. Если пул меньше `partner_crm.pool_low_threshold` И есть кому работать —
   зовёт `service.refill_pool(refill_batch_size)`.

Все три ключа читаются из таблицы `settings` на каждом тике —
оператор может править значения через UI без рестарта.

Логику дозалива не дублирует — делегирует `PartnerCrmService.refill_pool`,
который сам публикует событие `partner_crm.pool_refilled`.
"""
from __future__ import annotations

import asyncio
import logging

from core import db
from .errors import PartnerCrmError
from .service import PartnerCrmService

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SEC = 60
DEFAULT_POOL_LOW_THRESHOLD = 10
DEFAULT_REFILL_BATCH_SIZE = 50


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


async def _has_active_crm_worker() -> bool:
    """
    Есть ли смысл дозаливать пул? Да — если хотя бы один воркер с
    lead_source=partner_crm и включённым автозапуском.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            """
            SELECT 1 FROM worker_auto_runners r
            JOIN accounts a ON a.id = r.account_id
            WHERE r.is_running = TRUE
              AND a.lead_source = 'partner_crm'
              AND a.is_active = TRUE
            LIMIT 1
            """,
        )
    return row is not None


class PartnerCrmPoller:
    """
    Фоновый процесс. Создаётся в lifespan после PartnerCrmService,
    останавливается перед ним.
    """

    def __init__(self, service: PartnerCrmService) -> None:
        self._service = service
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        log.info("partner_crm poller started")
        # Если модуль не сконфигурирован — просто ничего не делаем, но
        # таска остаётся живой; если позже допишут .env и перезапустят —
        # после рестарта подхватится.
        while not self._stop_event.is_set():
            try:
                interval = await _get_int_setting(
                    "partner_crm.poll_interval_sec", DEFAULT_POLL_INTERVAL_SEC,
                )
                await self._tick()
            except asyncio.CancelledError:
                log.info("partner_crm poller cancelled")
                raise
            except Exception:
                log.exception("partner_crm poller tick error")
                interval = DEFAULT_POLL_INTERVAL_SEC

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=max(1, interval),
                )
            except asyncio.TimeoutError:
                pass  # просто следующий тик

    async def stop(self) -> None:
        self._stop_event.set()

    # ─── Тик ─────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        if not self._service.is_configured():
            return  # .env пустой — молча ждём

        threshold = await _get_int_setting(
            "partner_crm.pool_low_threshold", DEFAULT_POOL_LOW_THRESHOLD,
        )
        batch = await _get_int_setting(
            "partner_crm.refill_batch_size", DEFAULT_REFILL_BATCH_SIZE,
        )

        pool_size = await self._service.pool_size()
        if pool_size >= threshold:
            return

        if not await _has_active_crm_worker():
            # Нет активных потребителей — не дозаливаем (иначе будем тащить
            # лидов из CRM в пустую, нагружать партнёрский лимит).
            return

        log.info(
            "partner_crm: pool_size=%d < threshold=%d, refilling %d leads",
            pool_size, threshold, batch,
        )
        try:
            await self._service.refill_pool(batch)
        except PartnerCrmError as e:
            log.warning("partner_crm: refill failed: %s", e)
