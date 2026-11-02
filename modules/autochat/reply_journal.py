"""Durable reply checkpoint. No transport, LLM, or automatic uncertain resend.

All writers lock dialog before batch, matching history's dialog upsert transaction.
No network operation is executed under these short database transactions.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from core import db


async def enabled(key: str) -> bool:
    async with db.get_pool().acquire() as conn:
        value = await conn.fetchval("SELECT value FROM settings WHERE key=$1", key)
    return str(value).lower() in ("true", "1", "yes")


def unpack(row):
    if row is None:
        return None
    result = dict(row)
    if isinstance(result.get("segments"), str):
        result["segments"] = json.loads(result["segments"])
    return result


@asynccontextmanager
async def locked(dialog_id):
    async with db.get_pool().acquire() as conn, conn.transaction():
        await conn.fetchval("SELECT id FROM dialogs WHERE id=$1 FOR UPDATE", dialog_id)
        yield conn


async def latest(conn, dialog_id):
    return await conn.fetchrow("""SELECT id, telegram_message_id, date, is_outgoing
        FROM messages WHERE dialog_id=$1 AND deleted_at IS NULL
        ORDER BY date DESC, telegram_message_id DESC LIMIT 1""", dialog_id)


async def permitted(conn, session_id, dialog_id):
    return bool(await conn.fetchval("""SELECT EXISTS(
        SELECT 1 FROM autochat_sessions s JOIN dialogs d ON d.id=s.dialog_id
        LEFT JOIN dialog_statuses st ON st.id=d.status_id
        WHERE s.id=$1 AND d.id=$2 AND s.status='active'
          AND NOT COALESCE(st.closes_dialog,false)
          AND NOT EXISTS(SELECT 1 FROM report_moderation m WHERE m.dialog_id=d.id)
          AND NOT EXISTS(SELECT 1 FROM lead_evaluations e WHERE e.dialog_id=d.id)
        )""", session_id, dialog_id))


async def actionable(conn, dialog_id):
    msg = await latest(conn, dialog_id)
    if not msg or not msg["is_outgoing"]:
        return msg
    incoming = await conn.fetchrow("""SELECT id, telegram_message_id, date, is_outgoing
        FROM messages WHERE dialog_id=$1 AND NOT is_outgoing AND deleted_at IS NULL
        ORDER BY date DESC,telegram_message_id DESC LIMIT 1""", dialog_id)
    previous = unpack(await conn.fetchrow("""SELECT * FROM autochat_reply_batches
        WHERE dialog_id=$1 AND segments @> $2::jsonb ORDER BY id DESC LIMIT 1""",
        dialog_id, json.dumps([{"telegram_message_id": msg["telegram_message_id"]}])))
    # A response to an older input may finish after the new input arrived.
    # That send is not evidence of processing the newer input.
    if incoming and previous and previous["inbound_message_id"] != incoming["id"]:
        if msg["telegram_message_id"] in [s.get("telegram_message_id") for s in previous["segments"]]:
            return incoming
    return msg


async def begin(session_id, dialog_id, account_id, *, manual=False, reuse_intent=False):
    async with locked(dialog_id) as conn:
        return await stage_input(conn, session_id, dialog_id, account_id,
                                 manual=manual, reuse_intent=reuse_intent, check_history=True)


async def stage_input(conn, session_id, dialog_id, account_id, *, manual=False, reuse_intent=False, check_history=False):
    """Caller holds the dialog lock; history uses this before committing inbound."""
    if not await permitted(conn, session_id, dialog_id):
        return None
    msg = await latest(conn, dialog_id) if manual else await actionable(conn, dialog_id)
    if msg is None or (msg["is_outgoing"] and not manual):
        return None
    if not manual and await conn.fetchval("""SELECT 1 FROM autochat_reply_batches
        WHERE dialog_id=$1 AND inbound_message_id=$2 AND phase='complete' LIMIT 1""", dialog_id, msg["id"]):
        return None
    previous = unpack(await conn.fetchrow("""SELECT * FROM autochat_reply_batches b
        WHERE dialog_id=$1 AND EXISTS(SELECT 1 FROM jsonb_array_elements(b.segments) seg
            WHERE seg ? 'telegram_message_id') ORDER BY id DESC LIMIT 1""", dialog_id))
    if previous and check_history:
        sent_ids = [s["telegram_message_id"] for s in previous["segments"] if s.get("telegram_message_id")]
        if sent_ids:
            saved = await conn.fetchval("""SELECT count(*) FROM messages
                WHERE dialog_id=$1 AND telegram_message_id=ANY($2::bigint[])""", dialog_id, sent_ids)
            if saved != len(sent_ids):
                return None
    current = await conn.fetchrow("""SELECT * FROM autochat_reply_batches
        WHERE dialog_id=$1 AND phase IN
        ('generating','ready','sending','finishing','review') FOR UPDATE""", dialog_id)
    if current:
        # An unknown send is never superseded even by a new input.
        retryable_generation = current["phase"] == "review" and current["last_error"] in ("generation_failed", "empty_generation")
        if current["phase"] in ("sending", "finishing") or (current["phase"] == "review" and not retryable_generation):
            return None
        if current["inbound_message_id"] == msg["id"]:
            if not (retryable_generation and manual):
                return unpack(current) if reuse_intent and current["phase"] == "generating" else None
        await conn.execute("""UPDATE autochat_reply_batches SET phase='superseded',
            updated_at=NOW() WHERE id=$1""", current["id"])
    row = await conn.fetchrow("""INSERT INTO autochat_reply_batches
        (session_id,dialog_id,account_id,inbound_message_id,phase)
        VALUES($1,$2,$3,$4,'generating') RETURNING *""",
        session_id, dialog_id, account_id, msg["id"])
    return unpack(row)


async def current_input(conn, batch):
    """Ignore only our own confirmed segments; any other newer message invalidates work."""
    row = await latest(conn, batch["dialog_id"])
    if row is None:
        return False
    if row["id"] == batch["inbound_message_id"]:
        return True
    sent_ids = [s.get("telegram_message_id") for s in unpack(batch)["segments"]]
    if row["is_outgoing"] and row["telegram_message_id"] in sent_ids:
        return not await conn.fetchval("""SELECT EXISTS(
            SELECT 1 FROM messages m JOIN messages anchor ON anchor.id=$2
            WHERE m.dialog_id=$1 AND m.deleted_at IS NULL AND NOT m.is_outgoing
              AND (m.date,m.telegram_message_id)>(anchor.date,anchor.telegram_message_id))""",
            batch["dialog_id"], batch["inbound_message_id"])
    anchor = await actionable(conn, batch["dialog_id"])
    return bool(anchor and not anchor["is_outgoing"] and anchor["id"] == batch["inbound_message_id"])


async def store_result(batch, segments, finish_requested, parent_id):
    async with locked(batch["dialog_id"]) as conn:
        row = await conn.fetchrow("SELECT * FROM autochat_reply_batches WHERE id=$1 FOR UPDATE", batch["id"])
        if not row or row["phase"] != "generating":
            return None
        if not await permitted(conn, row["session_id"], row["dialog_id"]) or not await current_input(conn, row):
            await conn.execute("UPDATE autochat_reply_batches SET phase='superseded',updated_at=NOW() WHERE id=$1", row["id"])
            return None
        phase = "ready" if segments or finish_requested else "review"
        result = await conn.fetchrow("""UPDATE autochat_reply_batches
            SET phase=$2, segments=$3::jsonb, finish_requested=$4, parent_id=$5,
                last_error=$6, updated_at=NOW() WHERE id=$1 RETURNING *""",
            row["id"], phase, json.dumps([{"text": text} for text in segments]),
            finish_requested, parent_id, None if phase == "ready" else "empty_generation")
        return unpack(result)


async def before_send(batch_id, dialog_id, index):
    async with locked(dialog_id) as conn:
        row = await conn.fetchrow("SELECT * FROM autochat_reply_batches WHERE id=$1 FOR UPDATE", batch_id)
        if not row or row["phase"] != "ready" or row["next_index"] != index:
            return False
        if not await permitted(conn, row["session_id"], dialog_id) or not await current_input(conn, row):
            await conn.execute("UPDATE autochat_reply_batches SET phase='superseded',updated_at=NOW() WHERE id=$1", batch_id)
            return False
        await conn.execute("UPDATE autochat_reply_batches SET phase='sending',updated_at=NOW() WHERE id=$1", batch_id)
        return True


async def confirm_send(batch_id, dialog_id, index, telegram_message_id):
    if telegram_message_id is None:
        await review(batch_id, dialog_id, "missing_delivery_id")
        raise RuntimeError("Telegram delivery ID missing; reply requires review")
    async with locked(dialog_id) as conn:
        row = unpack(await conn.fetchrow("SELECT * FROM autochat_reply_batches WHERE id=$1 FOR UPDATE", batch_id))
        if row is None or row["phase"] != "sending" or row["next_index"] != index:
            raise RuntimeError("Reply checkpoint changed during send")
        row["segments"][index]["telegram_message_id"] = int(telegram_message_id)
        done = index + 1 == len(row["segments"])
        changed_input = not await current_input(conn, row)
        phase = "superseded" if changed_input else ("complete" if done and not row["finish_requested"] else "ready")
        await conn.execute("""UPDATE autochat_reply_batches SET phase=$2,segments=$3::jsonb,
            next_index=$4,updated_at=NOW() WHERE id=$1""", batch_id, phase,
            json.dumps(row["segments"]), index + 1)
        if changed_input:
            # Persist the new intent before leaving the RPC acknowledgement transaction.
            # Its LLM claim will wait for the confirmed outgoing history to be saved.
            await stage_input(conn, row["session_id"], dialog_id, row["account_id"])


async def start_finish(batch_id, dialog_id):
    async with locked(dialog_id) as conn:
        row = unpack(await conn.fetchrow("SELECT * FROM autochat_reply_batches WHERE id=$1 FOR UPDATE", batch_id))
        if not row or row["phase"] != "ready" or row["next_index"] != len(row["segments"]):
            return False
        sent_ids = [s["telegram_message_id"] for s in row["segments"] if s.get("telegram_message_id")]
        if sent_ids:
            count = await conn.fetchval("""SELECT count(*) FROM messages WHERE dialog_id=$1
                AND telegram_message_id=ANY($2::bigint[]) AND deleted_at IS NULL""", dialog_id, sent_ids)
            if count != len(sent_ids):
                return False
        if not await permitted(conn, row["session_id"], dialog_id) or not await current_input(conn, row):
            await conn.execute("UPDATE autochat_reply_batches SET phase='superseded',updated_at=NOW() WHERE id=$1", batch_id)
            return False
        await conn.execute("UPDATE autochat_reply_batches SET phase='finishing',updated_at=NOW() WHERE id=$1", batch_id)
        return True


async def finish_confirmed(batch_id, dialog_id):
    async with locked(dialog_id) as conn:
        row = unpack(await conn.fetchrow("SELECT * FROM autochat_reply_batches WHERE id=$1 FOR UPDATE", batch_id))
        if row is None or row["phase"] != "finishing":
            return
        changed = not await current_input(conn, row)
        await conn.execute("""UPDATE autochat_reply_batches SET phase=$2,updated_at=NOW()
            WHERE id=$1 AND phase='finishing'""", batch_id, "superseded" if changed else "complete")
        if changed:
            await stage_input(conn, row["session_id"], dialog_id, row["account_id"])


async def review(batch_id, dialog_id, reason):
    async with locked(dialog_id) as conn:
        await conn.execute("""UPDATE autochat_reply_batches SET phase='review',last_error=$2,
            updated_at=NOW() WHERE id=$1 AND phase NOT IN ('complete','superseded')""", batch_id, reason)


async def pending(session_id):
    async with db.get_pool().acquire() as conn:
        return unpack(await conn.fetchrow("""SELECT * FROM autochat_reply_batches
            WHERE session_id=$1 AND phase IN ('generating','ready','sending','finishing','review')
            ORDER BY id DESC LIMIT 1""", session_id))


async def abandon_generation(batch):
    """Only an intent with no persisted output is safe to regenerate after a crash."""
    async with locked(batch["dialog_id"]) as conn:
        return await conn.fetchval("""UPDATE autochat_reply_batches
            SET phase='superseded',updated_at=NOW()
            WHERE id=$1 AND phase='generating' RETURNING id""", batch["id"])
