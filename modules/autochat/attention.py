"""Bounded, read-only diagnostics; an attention item never authorizes a send."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from core import db

MAX_PAGE_SIZE = 500
LEGACY_RECOVERY_SECONDS = 6 * 3600
DISCOVERY_GRACE_SECONDS = 60

# Page dialogs before lateral lookups. No events_archive scans or message bodies.
# Empty pages of candidates are not evidence that the entire database is clear.
_PAGE_SQL = """
WITH page AS MATERIALIZED (
    SELECT d.id, d.account_id, d.status_id
    FROM dialogs d WHERE d.id > $1
      AND ($2::integer IS NULL OR d.account_id = $2)
      AND EXISTS (SELECT 1 FROM autochat_sessions s WHERE s.dialog_id = d.id)
    ORDER BY d.id LIMIT $3
)
SELECT d.id AS dialog_id, d.account_id,
       COALESCE(ds.closes_dialog, FALSE) AS closes_dialog,
       s.id AS session_id, s.status AS session_status,
       li.id AS inbound_message_id, li.telegram_message_id,
       li.date AS inbound_at, li.saved_at AS discovered_at, lm.is_outgoing,
       EXISTS (
           SELECT 1 FROM media md JOIN messages mm ON mm.id = md.message_id
           WHERE mm.dialog_id = d.id AND mm.deleted_at IS NULL
             AND (md.transcription_status = 'pending' OR md.description_status = 'pending')
       ) AS pending_media,
       e.evaluated_at, e.parse_succeeded AS evaluation_parse_succeeded,
       e.verdict AS evaluation_verdict,
       rm.dialog_id IS NOT NULL AS has_moderation,
       r.created_at AS report_created_at,
       b.id AS batch_id, b.inbound_message_id AS batch_inbound_message_id,
       b.phase AS batch_phase, b.finish_requested, b.updated_at AS batch_updated_at,
       CASE WHEN b.last_error IN ('generation_failed', 'empty_generation',
            'delivery_unknown', 'missing_delivery_id', 'finish_delivery_unknown')
            THEN b.last_error END AS batch_error_kind
FROM page d
LEFT JOIN dialog_statuses ds ON ds.id = d.status_id
LEFT JOIN LATERAL (
    SELECT id, status FROM autochat_sessions WHERE dialog_id = d.id ORDER BY id DESC LIMIT 1
) s ON TRUE
LEFT JOIN LATERAL (
    SELECT id, telegram_message_id, date, saved_at, is_outgoing
    FROM messages WHERE dialog_id = d.id AND deleted_at IS NULL
    ORDER BY date DESC, telegram_message_id DESC LIMIT 1
) lm ON TRUE
LEFT JOIN LATERAL (
    SELECT id, telegram_message_id, date, saved_at
    FROM messages WHERE dialog_id = d.id AND deleted_at IS NULL AND NOT is_outgoing
    ORDER BY date DESC, telegram_message_id DESC LIMIT 1
) li ON TRUE
LEFT JOIN lead_evaluations e ON e.dialog_id = d.id
LEFT JOIN report_moderation rm ON rm.dialog_id = d.id
LEFT JOIN autochat_reports r ON r.session_id = s.id
LEFT JOIN LATERAL (
    SELECT id, inbound_message_id, phase, finish_requested, updated_at, last_error
    FROM autochat_reply_batches WHERE session_id = s.id ORDER BY id DESC LIMIT 1
) b ON TRUE
ORDER BY d.id
"""

_SETTINGS = {
    "autochat.enter_delay_short_sec": 15,
    "autochat.enter_delay_mid_sec": 60,
    "autochat.enter_delay_long_sec": 120,
    "autochat.reply_timer_sec": 30,
    "autochat.openrouter_retries": 2,
}


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    if isinstance(value, str):
        try:
            return _dt(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            pass
    return None


def _elapsed(now: datetime, value: Any) -> int | None:
    instant = _dt(value)
    return max(0, int((now - instant).total_seconds())) if instant else None


def _iso(value: Any) -> str | None:
    instant = _dt(value)
    return instant.isoformat() if instant else None


def classify_attention(
    row: Mapping[str, Any], live: Mapping[str, Any] | None, *, now: datetime,
    wait_grace_seconds: int = 210, generation_grace_seconds: int = 660,
) -> dict[str, Any]:
    """Classify metadata without interpreting text, resending or reopening.

    A batch only describes the input it records. Missing live state is unknown;
    an empty queue does not prove idle because its sender may have taken work.
    """
    now = _dt(now) or datetime.now(timezone.utc)
    inbound_at = _dt(row.get("inbound_at"))
    discovered_at = _dt(row.get("discovered_at"))
    if discovered_at is None and live is not None:
        if live.get("last_inbound_message_id") == row.get("inbound_message_id"):
            discovered_at = _dt(live.get("last_inbound_seen_at"))
    age, discovery_age = _elapsed(now, inbound_at), _elapsed(now, discovered_at)
    batch_current = (row.get("batch_id") is not None and
                     row.get("batch_inbound_message_id") == row.get("inbound_message_id"))
    phase = row.get("batch_phase") if batch_current else None
    result = {
        "account_id": row.get("account_id"), "dialog_id": row.get("dialog_id"),
        "session_id": row.get("session_id"), "session_status": row.get("session_status"),
        "inbound_message_id": row.get("inbound_message_id"),
        "telegram_message_id": row.get("telegram_message_id"),
        "inbound_at": _iso(inbound_at), "discovered_at": _iso(discovered_at),
        "inbound_age_seconds": age, "discovery_age_seconds": discovery_age,
        "outside_legacy_recovery_window": age is not None and age > LEGACY_RECOVERY_SECONDS,
        "live_state_known": live is not None,
        "worker_connected": live.get("worker_connected") if live is not None else None,
        "closes_dialog": bool(row.get("closes_dialog")),
        "has_moderation": bool(row.get("has_moderation")),
        "has_report_record": row.get("report_created_at") is not None,
        "batch_id": row.get("batch_id") if batch_current else None, "batch_phase": phase,
    }

    def finish(reason: str, review: bool = False) -> dict[str, Any]:
        return {**result, "reason": reason, "needs_review": review}

    unfinished_batch = phase in {"generating", "ready", "sending", "finishing", "review"}
    finish_awaits_evaluation = phase == "complete" and row.get("finish_requested")
    if inbound_at is None or (row.get("is_outgoing") and not unfinished_batch and not finish_awaits_evaluation):
        return finish("no_unanswered_inbound")
    evaluated_at = _dt(row.get("evaluated_at"))
    if (evaluated_at is not None and evaluated_at >= inbound_at
        and (discovered_at is None or evaluated_at >= discovered_at)
        and row.get("evaluation_parse_succeeded") and row.get("evaluation_verdict") is not None
        and (row.get("closes_dialog") or row.get("has_moderation"))):
        return finish("completed_by_evaluation")
    if row.get("has_moderation"):
        return finish("moderated_followup", True)
    if phase in {"finishing", "complete"} and row.get("finish_requested"):
        elapsed = _elapsed(now, row.get("batch_updated_at"))
        overdue = elapsed is not None and elapsed > 900
        reason = "completion" if phase == "finishing" else "evaluation"
        return finish(reason + ("_overdue" if overdue else "_pending"), overdue)
    if phase == "review":
        error_kind = row.get("batch_error_kind")
        if error_kind in {"generation_failed", "empty_generation"}:
            return finish("generation_failed", True)
        if error_kind in {"delivery_unknown", "missing_delivery_id"}:
            return finish("send_outcome_unknown", True)
        return finish("reply_requires_review", True)
    if phase == "sending" and (live is None or not live.get("sending")):
        return finish("send_outcome_unknown", True)
    if row.get("session_status") in {"stopped", "failed", "paused"}:
        return finish("session_" + row["session_status"], True)
    if row.get("closes_dialog"):
        return finish("terminal_dialog_review", True)
    if discovery_age is not None and discovery_age < DISCOVERY_GRACE_SECONDS:
        return finish("history_sync_grace")
    if live is None:
        return finish("live_state_unknown", True)
    if live.get("worker_connected") is False:
        return finish("worker_disconnected", True)
    if live.get("stopped") or live.get("registered") is False:
        return finish("session_not_registered", True)
    phase_age = _elapsed(now, live.get("phase_started_at"))
    if phase_age is None:
        phase_age = discovery_age

    def pending(reason: str, budget: int) -> dict[str, Any]:
        overdue = phase_age is not None and phase_age > budget
        return finish(reason + ("_overdue" if overdue else "_pending"), overdue)

    if live.get("sending") or live.get("queued_segments", 0):
        return pending("delivery", 300)
    if live.get("waiting_media"):
        return pending("media", 300 + wait_grace_seconds)
    if live.get("generating"):
        return pending("generation", generation_grace_seconds)
    if live.get("entering") or live.get("reply_waiting") or live.get("event_queue_size", 0):
        return pending("scheduled_wait", wait_grace_seconds)
    if live.get("last_error_kind") == "generation_failed":
        return finish("generation_failed", True)
    if row.get("pending_media"):
        return pending("media", 300 + wait_grace_seconds)
    if result["outside_legacy_recovery_window"]:
        return finish("outside_recovery_window", True)
    if discovery_age is None:
        return finish("discovery_time_unknown", True)
    if discovery_age <= wait_grace_seconds:
        return finish("expected_pause")
    return finish("processing_unconfirmed", True)


async def inspect_attention(
    get_live_state: Callable[[int], Mapping[str, Any] | None] | None = None, *,
    account_id: int | None = None, limit: int = 100, after_dialog_id: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One explicit keyset page. Callback only reads memory; DB errors propagate."""
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    if after_dialog_id < 0 or (account_id is not None and account_id <= 0):
        raise ValueError("invalid account or cursor")
    observed_at = _dt(now) or datetime.now(timezone.utc)
    async with db.get_pool().acquire() as conn:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            config = dict(_SETTINGS)
            settings_rows = await conn.fetch(
                "SELECT key, value FROM settings WHERE key = ANY($1::text[])",
                list(config), timeout=10,
            )
            for setting in settings_rows:
                try:
                    config[setting["key"]] = max(0, int(setting["value"]))
                except (ValueError, TypeError):
                    pass
            rows = await conn.fetch(_PAGE_SQL, after_dialog_id, account_id, limit + 1, timeout=10)
    has_more, page = len(rows) > limit, rows[:limit]
    wait_grace = max(config[k] for k in config if "enter_delay" in k)
    wait_grace += config["autochat.reply_timer_sec"] + DISCOVERY_GRACE_SECONDS
    generation_grace = (config["autochat.openrouter_retries"] + 1) * 210 + 30
    items, snapshot_errors = [], 0
    for row in page:
        if row["inbound_at"] is None:
            continue
        live = None
        if get_live_state is not None and row["session_id"] is not None:
            try:
                live = get_live_state(row["session_id"])
            except Exception:
                snapshot_errors += 1
        item = classify_attention(row, live, now=observed_at,
                     wait_grace_seconds=wait_grace, generation_grace_seconds=generation_grace)
        if item["reason"] != "no_unanswered_inbound":
            items.append(item)
    return {
        "observed_at": observed_at.isoformat(), "read_only": True, "scope": "dialog_page",
        "account_id": account_id, "scanned_dialogs": len(page), "has_more": has_more,
        "next_after_dialog_id": page[-1]["dialog_id"] if page else after_dialog_id,
        "snapshot_errors": snapshot_errors,
        "review_count": sum(item["needs_review"] for item in items),
        "counts": dict(Counter(item["reason"] for item in items)), "items": items,
        "limitations": [
            "Counts cover this page only; follow the cursor while has_more is true.",
            "Saved history is not a fresh Telegram delivery verification.",
            "Report records do not prove an HTML object or group delivery exists.",
            "Legacy messages may have unknown discovery time; no automatic action is taken.",
        ],
    }
