"""Terminal CRM delivery with a bounded durable receipt per dialogue.

Only status events create work; no history scan or backfill. First sends retain
existing behaviour. Retrying persisted work is separately enabled in settings.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from core import bus, db
from core.events import EventType
from .errors import CrmHttpError, PartnerCrmError
from .guard import dialog_guard
from .service import PartnerCrmService

log = logging.getLogger(__name__)
FINALIZER_GROUP = "partner-crm-finalizer"
FINALIZER_CONSUMER = "partner-crm-finalizer-1"
MAX_ATTEMPTS = 3
BACKOFF_SEC = (15 * 60, 60 * 60)
RETRY_TICK_SEC = 60


def retryable(exc: PartnerCrmError) -> bool:
    return isinstance(exc, CrmHttpError) and (
        exc.status is None or exc.status == 429 or 500 <= exc.status < 600
    )


def reconciliation_action(card: dict[str, Any], desired: str, owner_id: int | None) -> str:
    """Fail closed when either ownership or the existing result is unclear."""
    status = card.get("lead_status")
    if status == desired:
        return "confirmed"
    if not owner_id or str(card.get("assigned_user_id")) != str(owner_id):
        return "owner_changed_or_unknown"
    if status != "in_progress":
        return "crm_status_changed_or_unknown"
    return "send"


class PartnerCrmFinalizer:
    def __init__(self, service: PartnerCrmService) -> None:
        self._service = service
        self._stop_event = asyncio.Event()
        self._next_retry_tick = 0.0

    async def run(self) -> None:
        await bus.ensure_group(FINALIZER_GROUP)
        log.info("PartnerCrmFinalizer loop started")
        while not self._stop_event.is_set():
            try:
                batch = await bus.read_group(
                    FINALIZER_GROUP, FINALIZER_CONSUMER, count=20, block_ms=5000,
                )
                ack_ids: list[str] = []
                for stream_id, event in batch:
                    try:
                        await self._handle(event)
                        ack_ids.append(stream_id)
                        await bus.record_success(FINALIZER_GROUP, stream_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.exception("partner_crm finalizer: failed event %s", event.get("id"))
                        if await bus.record_failure(FINALIZER_GROUP, stream_id, event, exc):
                            ack_ids.append(stream_id)
                if ack_ids:
                    await bus.ack_group(FINALIZER_GROUP, ack_ids)
                now = asyncio.get_running_loop().time()
                if now >= self._next_retry_tick:
                    self._next_retry_tick = now + RETRY_TICK_SEC
                    await self._retry_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("PartnerCrmFinalizer loop error")
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stop_event.set()

    async def _snapshot(self, dialog_id: int) -> dict[str, Any] | None:
        async with db.get_pool().acquire() as conn:
            row = await conn.fetchrow("""
                SELECT d.id AS dialog_id, d.account_id, d.username, d.from_crm,
                       d.status_id, d.crm_finalization_version,
                       d.partner_crm_terminal_reason AS stored_reason,
                       ds.partner_terminal_status AS bubble_terminal,
                       EXISTS (SELECT 1 FROM report_moderation m
                               WHERE m.dialog_id=d.id) AS has_moderation
                FROM dialogs d LEFT JOIN dialog_statuses ds ON ds.id=d.status_id
                WHERE d.id=$1
            """, dialog_id)
        return dict(row) if row is not None else None

    @staticmethod
    def _desired(row: dict[str, Any] | None) -> str | None:
        if not row or not row["from_crm"] or row["has_moderation"]:
            return None
        if not row["bubble_terminal"] or not row["username"]:
            return None
        return row["stored_reason"] or row["bubble_terminal"]

    async def _handle(self, event: dict[str, Any]) -> None:
        if event.get("type") != EventType.DIALOG_STATUS_CHANGED:
            return
        dialog_id = (event.get("data") or {}).get("dialog_id")
        if dialog_id is None or not self._service.is_configured():
            return
        dialog_id = int(dialog_id)
        async with dialog_guard(dialog_id):
            snapshot = await self._snapshot(dialog_id)
            desired = self._desired(snapshot)
            if desired is None:
                # A newer intermediate status/moderation supersedes pending work.
                async with db.get_pool().acquire() as conn:
                    await conn.execute("""
                        UPDATE partner_crm_finalizations SET state='superseded',
                            next_attempt_at=NULL, updated_at=NOW()
                        WHERE dialog_id=$1 AND state IN ('pending','sending','retry')
                    """, dialog_id)
                return
            async with db.get_pool().acquire() as conn:
                old = await conn.fetchrow(
                    "SELECT * FROM partner_crm_finalizations WHERE dialog_id=$1", dialog_id,
                )
                if old is not None:
                    if old["desired_version"] == snapshot["crm_finalization_version"]:
                        return  # Duplicate event never bypasses delay or manual review.
                    if (old["state"] == "confirmed" and snapshot["stored_reason"] is None
                            and old["source_status_id"] == snapshot["status_id"]
                            and old["desired_status"] == desired
                            and old["username"] == snapshot["username"]):
                        return  # Our reason-clear changes version, not the decision.
                needs_reconcile = old is not None and int(old["attempts"]) > 0
                await conn.execute("""
                    INSERT INTO partner_crm_finalizations
                        (dialog_id, desired_version, desired_status, source_status_id,
                         source_reason, account_id, username, next_attempt_at, needs_reconcile)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8)
                    ON CONFLICT(dialog_id) DO UPDATE SET
                        desired_version=EXCLUDED.desired_version,
                        desired_status=EXCLUDED.desired_status,
                        source_status_id=EXCLUDED.source_status_id,
                        source_reason=EXCLUDED.source_reason,
                        account_id=EXCLUDED.account_id, username=EXCLUDED.username,
                        state='pending', attempts=0, needs_reconcile=EXCLUDED.needs_reconcile,
                        next_attempt_at=NOW(), confirmed_status=NULL, confirmed_at=NULL,
                        last_error=NULL, updated_at=NOW()
                """, dialog_id, snapshot["crm_finalization_version"], desired,
                    snapshot["status_id"], snapshot["stored_reason"],
                    snapshot["account_id"], snapshot["username"], needs_reconcile)
            await self._attempt(dialog_id)

    async def _retry_due(self) -> None:
        if not self._service.is_configured():
            return
        async with db.get_pool().acquire() as conn:
            enabled = await conn.fetchval(
                "SELECT value FROM settings WHERE key='partner_crm.finalizer_retry_enabled'",
            )
            if str(enabled).lower() not in ("true", "1", "yes"):
                return
            due = await conn.fetch("""
                SELECT dialog_id FROM partner_crm_finalizations
                WHERE state IN ('pending','sending','retry') AND next_attempt_at <= NOW()
                ORDER BY next_attempt_at LIMIT 20
            """)
        for row in due:
            if self._stop_event.is_set():
                return
            async with dialog_guard(int(row["dialog_id"])):
                await self._attempt(int(row["dialog_id"]))

    async def _set_state(self, job: dict[str, Any], state: str, error: str | None) -> None:
        async with db.get_pool().acquire() as conn:
            await conn.execute("""
                UPDATE partner_crm_finalizations SET state=$3, last_error=$4,
                    next_attempt_at=NULL, updated_at=NOW()
                WHERE dialog_id=$1 AND desired_version=$2
            """, job["dialog_id"], job["desired_version"], state, error)

    async def _attempt(self, dialog_id: int) -> None:
        async with db.get_pool().acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM partner_crm_finalizations WHERE dialog_id=$1
                  AND state IN ('pending','sending','retry') AND next_attempt_at <= NOW()
            """, dialog_id)
        if row is None:
            return
        job = dict(row)
        current = await self._snapshot(dialog_id)
        if not self._matches(job, current):
            await self._set_state(job, "superseded", "newer_local_decision")
            return
        # Persist 'sending' BEFORE HTTP: crash/cancel requires read-before-retry.
        async with db.get_pool().acquire() as conn:
            await conn.execute("""
                UPDATE partner_crm_finalizations SET state='sending',
                    attempts=attempts+1, needs_reconcile=TRUE, next_attempt_at=NOW()+INTERVAL '5 minutes',
                    updated_at=NOW() WHERE dialog_id=$1 AND desired_version=$2
            """, dialog_id, job["desired_version"])
        previous_attempts = job["attempts"]
        job["attempts"] += 1
        try:
            if job["needs_reconcile"] or job["state"] == "sending" or previous_attempts:
                async with db.get_pool().acquire() as conn:
                    value = await conn.fetchval(
                        "SELECT value FROM settings WHERE key='partner_crm.retry_owner_id'",
                    )
                try:
                    owner_id = int(value)
                except (TypeError, ValueError):
                    owner_id = None
                card = await self._service.client.get_lead_card(job["username"])
                action = reconciliation_action(card, job["desired_status"], owner_id)
                if action == "confirmed":
                    if not self._matches(job, await self._snapshot(dialog_id)):
                        await self._set_state(job, "superseded", "newer_local_decision")
                        return
                    # POST may have succeeded before its receipt/local cleanup
                    # was lost. The native cleanup is idempotent and does not POST.
                    await self._service._cleanup_after_terminal(
                        job["username"], job["desired_status"],
                    )
                    await self._confirm(job)
                    return
                if action != "send":
                    await self._set_state(job, "review", action)
                    return
            if previous_attempts >= MAX_ATTEMPTS:
                await self._set_state(job, "review", "retry_limit")
                return
            # GET may take seconds; never push a stale local decision after it.
            if not self._matches(job, await self._snapshot(dialog_id)):
                await self._set_state(job, "superseded", "newer_local_decision")
                return
            await self._service.push_lead_finalized(
                account_id=job["account_id"], username=job["username"],
                terminal_status=job["desired_status"],
            )
        except PartnerCrmError as exc:
            await self._failed(job, exc)
            return
        await self._confirm(job)

    def _matches(self, job: dict[str, Any], row: dict[str, Any] | None) -> bool:
        return bool(row and row["crm_finalization_version"] == job["desired_version"]
                    and row["username"] == job["username"]
                    and self._desired(row) == job["desired_status"])

    async def _failed(self, job: dict[str, Any], exc: PartnerCrmError) -> None:
        # Store a safe classification, never response bodies/tokens/URLs.
        error = f"{exc.code}:{getattr(exc, 'status', None) or 'network'}"
        if not retryable(exc) or job["attempts"] >= MAX_ATTEMPTS:
            await self._set_state(job, "review", error)
            return
        delay = BACKOFF_SEC[min(max(job["attempts"] - 1, 0), len(BACKOFF_SEC) - 1)]
        async with db.get_pool().acquire() as conn:
            await conn.execute("""
                UPDATE partner_crm_finalizations SET state='retry', last_error=$3,
                    next_attempt_at=NOW()+$4*INTERVAL '1 second', updated_at=NOW()
                WHERE dialog_id=$1 AND desired_version=$2
            """, job["dialog_id"], job["desired_version"], error, delay)

    async def _confirm(self, job: dict[str, Any]) -> None:
        async with db.get_pool().acquire() as conn:
            async with conn.transaction():
                # The trigger's version detects all status/reason writers, including
                # a new reason that changed away and back while HTTP was in flight.
                cleared = await conn.fetchval("""
                    UPDATE dialogs SET partner_crm_terminal_reason=NULL, updated_at=NOW()
                    WHERE id=$1 AND crm_finalization_version=$2
                      AND status_id IS NOT DISTINCT FROM $3
                      AND partner_crm_terminal_reason IS NOT DISTINCT FROM $4
                      AND NOT EXISTS (SELECT 1 FROM report_moderation WHERE dialog_id=$1)
                    RETURNING id
                """, job["dialog_id"], job["desired_version"],
                    job["source_status_id"], job["source_reason"])
                await conn.execute("""
                    UPDATE partner_crm_finalizations SET state=$3,
                        confirmed_status=$4, confirmed_at=NOW(), next_attempt_at=NULL,
                        last_error=$5, updated_at=NOW()
                    WHERE dialog_id=$1 AND desired_version=$2
                """, job["dialog_id"], job["desired_version"],
                    "confirmed" if cleared is not None else "superseded",
                    job["desired_status"], None if cleared is not None else "newer_local_decision")
