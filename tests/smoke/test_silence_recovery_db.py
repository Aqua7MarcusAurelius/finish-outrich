"""Real SQL/row-lock tests, only against explicit disposable *_test database."""
from __future__ import annotations

import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import asyncpg

from core.events import EventType
from modules.dialog_statuses import service as statuses
from modules.history.service import HistoryService
from modules.lead_evaluator.silence_watcher import SilenceWatcher

DSN = os.environ.get("TEST_SILENCE_DSN", "")
ID = 500001


@unittest.skipUnless(DSN, "requires explicit disposable TEST_SILENCE_DSN")
class SilenceRecoveryDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not urlparse(DSN).path.endswith("_test"):
            raise RuntimeError("Refusing any database name not ending in _test")
        self.conn = await asyncpg.connect(DSN)
        self.tx = self.conn.transaction()
        await self.tx.start()

        @asynccontextmanager
        async def acquire():
            yield self.conn

        self.pool_patch = patch("core.db.get_pool", return_value=SimpleNamespace(acquire=acquire))
        self.pool_patch.start()
        self.publish = AsyncMock()
        self.bus_patch = patch("core.bus.publish", self.publish)
        self.bus_patch.start()
        await self.conn.execute("""
            INSERT INTO accounts(id,name,phone,proxy_primary,proxy_fallback)
            VALUES($1,'SILENCE_TEST','test_silence_500001','test://none','test://none');
        """, ID)
        self.ids = {r["label"]: r["id"] for r in await self.conn.fetch(
            "SELECT id,label FROM dialog_statuses")}
        await self.conn.execute("""
            INSERT INTO dialogs(id,account_id,telegram_user_id,status_id)
            VALUES($1,$1,$2,$3)
        """, ID, ID, self.ids["Первый контакт"])
        await self.conn.execute("""
            INSERT INTO autochat_sessions(id,account_id,dialog_id,telegram_user_id,
                target_username,system_prompt,initial_prompt,status)
            VALUES($1,$1,$1,$2,'silence_test','','','active')
        """, ID, ID)
        self.old_id = await self.message(1, outgoing=True, age_hours=60)
        self.watcher = SilenceWatcher()
        self.history = HistoryService()

    async def asyncTearDown(self):
        self.pool_patch.stop()
        self.bus_patch.stop()
        if self.tx is not None:
            await self.tx.rollback()
        else:
            await self.conn.execute("DELETE FROM accounts WHERE id=$1", ID)
        await self.conn.close()

    async def message(self, tg_id, *, outgoing, age_hours=0, fresh_discovery=False):
        date = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        saved = datetime.now(timezone.utc) if fresh_discovery else date
        return await self.conn.fetchval("""
            INSERT INTO messages(dialog_id,telegram_message_id,is_outgoing,date,saved_at)
            VALUES($1,$2,$3,$4,$5) RETURNING id
        """, ID, tg_id, outgoing, date, saved)

    async def close(self):
        return await self.watcher._try_transition(
            ID, [self.ids["Первый контакт"], self.ids["Обрыв"]],
            self.ids["Завершен провал"], 48, incoming_only=False,
            source="event:silence_timer.no_response",
        )

    async def incoming_event(self, tg_id=10, age_hours=0):
        await self.history._on_message_received({
            "id": "test-event", "account_id": ID,
            "data": {
                "telegram_user_id": ID, "telegram_message_id": tg_id,
                "is_outgoing": False, "text": "late reply",
                "date": (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(),
            },
        })

    async def test_silence_reason_status_and_watermark_commit_together(self):
        self.assertTrue(await self.close())
        row = await self.conn.fetchrow("SELECT * FROM dialogs WHERE id=$1", ID)
        self.assertEqual(row["status_id"], self.ids["Завершен провал"])
        self.assertEqual(row["partner_crm_terminal_reason"], "no_response")
        self.assertEqual(row["status_source"], "event:silence_timer.no_response")
        self.assertEqual(row["status_source_message_id"], self.old_id)
        self.publish.assert_awaited_once()

    async def test_unanswered_active_inbound_is_not_lead_silence(self):
        await self.message(2, outgoing=False, age_hours=50)
        self.assertFalse(await self.close())
        await self.conn.execute("UPDATE dialogs SET status_id=$2 WHERE id=$1", ID, self.ids["Общение"])
        self.assertFalse(await self.watcher._try_transition(
            ID, [self.ids["Общение"]], self.ids["Обрыв"], 4,
            incoming_only=True, source="event:silence_timer.obryv",
        ))
        self.publish.assert_not_awaited()

    async def test_prepared_or_uncertain_reply_blocks_silence(self):
        for phase in ("generating", "ready", "sending", "finishing", "review"):
            await self.conn.execute("DELETE FROM autochat_reply_batches WHERE dialog_id=$1", ID)
            await self.conn.execute("""INSERT INTO autochat_reply_batches
                (session_id,dialog_id,account_id,phase) VALUES($1,$1,$1,$2)""", ID, phase)
            self.assertFalse(await self.close(), phase)

    async def test_new_discovery_gets_grace_even_with_old_telegram_date(self):
        await self.message(2, outgoing=True, age_hours=55, fresh_discovery=True)
        self.assertFalse(await self.close())

    async def test_known_silence_late_inbound_reopens_with_saved_time(self):
        self.assertTrue(await self.close())
        self.publish.reset_mock()
        await self.incoming_event()
        row = await self.conn.fetchrow("SELECT * FROM dialogs WHERE id=$1", ID)
        self.assertEqual(row["status_id"], self.ids["Общение"])
        self.assertIsNone(row["partner_crm_terminal_reason"])
        saved = [c.kwargs for c in self.publish.await_args_list if c.kwargs["type"] == EventType.MESSAGE_SAVED]
        self.assertEqual(len(saved), 1)
        self.assertIsNotNone(saved[0]["data"]["saved_at"])
        self.assertIsNotNone(saved[0]["data"]["date"])
        count = self.publish.await_count
        await self.incoming_event()
        self.assertEqual(count, self.publish.await_count, "duplicate history must not repeat transition")

    async def test_confirmed_crm_silence_can_reopen_locally_after_reason_cleanup(self):
        self.assertTrue(await self.close())
        await self.conn.execute("UPDATE dialogs SET partner_crm_terminal_reason=NULL WHERE id=$1", ID)
        await self.incoming_event()
        self.assertEqual(await self.conn.fetchval("SELECT status_id FROM dialogs WHERE id=$1", ID),
                         self.ids["Общение"])

    async def test_history_stages_intent_before_publication_and_duplicate_ack(self):
        original_flag = await self.conn.fetchval(
            "SELECT value FROM settings WHERE key='autochat.reply_journal_enabled'",
        )
        await self.conn.execute("UPDATE settings SET value='true' WHERE key='autochat.reply_journal_enabled'")
        self.publish.side_effect = RuntimeError("Redis unavailable after database commit")
        with self.assertRaises(RuntimeError):
            await self.incoming_event()
        batch = await self.conn.fetchrow("SELECT * FROM autochat_reply_batches WHERE dialog_id=$1", ID)
        self.assertIsNotNone(batch)
        self.assertEqual(batch["phase"], "generating")
        self.assertEqual(batch["inbound_message_id"], await self.conn.fetchval(
            "SELECT id FROM messages WHERE dialog_id=$1 AND telegram_message_id=10", ID))
        # Commit the test fixture only after restoring the shared flag, then
        # verify the persisted intent from a separate DB connection.
        await self.conn.execute(
            "UPDATE settings SET value=$1 WHERE key='autochat.reply_journal_enabled'", original_flag,
        )
        await self.tx.commit()
        self.tx = None
        other = await asyncpg.connect(DSN)
        try:
            self.assertEqual(await other.fetchval(
                "SELECT phase FROM autochat_reply_batches WHERE dialog_id=$1", ID), "generating")
        finally:
            await other.close()
        self.publish.side_effect = None
        await self.incoming_event()
        self.assertEqual(await self.conn.fetchval(
            "SELECT COUNT(*) FROM autochat_reply_batches WHERE dialog_id=$1", ID), 1)

    async def test_feature_off_does_not_stage_intent(self):
        await self.conn.execute("UPDATE settings SET value='false' WHERE key='autochat.reply_journal_enabled'")
        await self.incoming_event()
        self.assertEqual(await self.conn.fetchval(
            "SELECT COUNT(*) FROM autochat_reply_batches WHERE dialog_id=$1", ID), 0)

    async def test_duplicate_historical_input_does_not_backfill_intent(self):
        await self.message(10, outgoing=False, age_hours=50)
        await self.conn.execute("UPDATE settings SET value='true' WHERE key='autochat.reply_journal_enabled'")
        await self.incoming_event()
        self.assertEqual(await self.conn.fetchval(
            "SELECT COUNT(*) FROM autochat_reply_batches WHERE dialog_id=$1", ID), 0)

    async def test_unknown_manual_and_stopped_sessions_stay_closed(self):
        self.assertTrue(await self.close())
        for index, (source, session_status) in enumerate([
            (None, "active"), ("manual", "active"),
            ("event:silence_timer.no_response", "stopped"),
            ("event:silence_timer.no_response", "failed"),
        ]):
            await self.conn.execute("UPDATE dialogs SET status_source=$2 WHERE id=$1", ID, source)
            await self.conn.execute("UPDATE autochat_sessions SET status=$2 WHERE id=$1", ID, session_status)
            await self.incoming_event(tg_id=10 + index)
            self.assertEqual(await self.conn.fetchval("SELECT status_id FROM dialogs WHERE id=$1", ID),
                             self.ids["Завершен провал"])

    async def test_old_import_is_not_late_inbound(self):
        self.assertTrue(await self.close())
        await self.incoming_event(age_hours=70)
        self.assertEqual(await self.conn.fetchval("SELECT status_id FROM dialogs WHERE id=$1", ID),
                         self.ids["Завершен провал"])

    async def test_evaluated_and_moderated_dialogs_are_protected(self):
        await self.conn.execute("INSERT INTO lead_evaluations(dialog_id,verdict) VALUES($1,'success')", ID)
        self.assertFalse(await self.close())
        await self.conn.execute("DELETE FROM lead_evaluations WHERE dialog_id=$1", ID)
        self.assertTrue(await self.close())
        await self.conn.execute("""INSERT INTO report_moderation
            (dialog_id,username,chat_id,message_id,choice) VALUES($1,'test',-500001,1,'take')""", ID)
        await self.incoming_event()
        self.assertEqual(await self.conn.fetchval("SELECT status_id FROM dialogs WHERE id=$1", ID),
                         self.ids["Завершен провал"])
        self.assertEqual(await self.conn.fetchval("SELECT choice FROM report_moderation WHERE dialog_id=$1", ID), "take")

    async def test_status_cas_preserves_new_decision_and_none_is_real_expectation(self):
        result = await statuses.set_dialog_status(
            ID, self.ids["Общение"], source="event:test", expected_status_id=None,
        )
        self.assertFalse(result["changed"])
        self.publish.assert_not_awaited()
        result = await statuses.set_dialog_status(ID, self.ids["Модерация"], source="manual")
        self.assertTrue(result["changed"])
        result = await statuses.set_dialog_status(
            ID, self.ids["Общение"], source="event:test", expected_status_id=self.ids["Первый контакт"],
        )
        self.assertFalse(result["changed"])
        self.assertEqual(result["status"]["id"], self.ids["Модерация"])
        self.assertEqual(await self.conn.fetchval("SELECT status_source_message_id FROM dialogs WHERE id=$1", ID), self.old_id)

    async def test_history_lock_beats_stale_silence_candidate(self):
        # Commit this one fixture so another real connection can contend.
        await self.tx.commit()
        self.tx = None
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            async with self.conn.transaction():
                await self.conn.fetchval("SELECT id FROM dialogs WHERE id=$1 FOR UPDATE", ID)
                with patch("core.db.get_pool", return_value=pool):
                    task = asyncio.create_task(self.close())
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
                    await self.message(2, outgoing=False)
            self.assertFalse(await asyncio.wait_for(task, timeout=2))
            self.assertIsNone(await self.conn.fetchval(
                "SELECT partner_crm_terminal_reason FROM dialogs WHERE id=$1", ID))
        finally:
            await pool.close()


if __name__ == "__main__":
    unittest.main()
