"""Actual PostgreSQL selector tests, opt-in and restricted to the isolated test DB.

OUTRICH_ISOLATED_SQL_TESTS=1, network outrich-recovery-test. Every test rolls back
its fixtures. No production settings, credentials, migrations or services used.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import unittest

import asyncpg
from modules.autochat.attention import _PAGE_SQL, classify_attention

NOW = datetime(2026, 10, 27, 15, tzinfo=timezone.utc)
ACCOUNT = 260001
DIALOG = 260010
SESSION = 260020


@unittest.skipUnless(os.getenv("OUTRICH_ISOLATED_SQL_TESTS") == "1", "isolated DB opt-in required")
class SelectorSqlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = await asyncpg.connect(host="outrich-recovery-test-db", port=5432,
            user="outrich_test", password="isolated_test_only", database="outrich_test")
        self.addAsyncCleanup(self.conn.close)
        self.assertEqual(await self.conn.fetchval("SELECT current_database()"), "outrich_test")
        self.tx = self.conn.transaction()
        await self.tx.start()
        await self.conn.execute("""INSERT INTO accounts(id,name,phone,proxy_primary,proxy_fallback)
            VALUES($1,'TEST_ATTENTION','+000260001','test','test')""", ACCOUNT)
        await self.dialog(DIALOG, SESSION)

    async def asyncTearDown(self):
        await self.tx.rollback()
        await self.conn.close()

    async def dialog(self, dialog_id, session_id):
        await self.conn.execute("""INSERT INTO dialogs(id,account_id,telegram_user_id)
            VALUES($1::integer,$2,$1::integer)""", dialog_id, ACCOUNT)
        await self.conn.execute("""INSERT INTO autochat_sessions
            (id,account_id,dialog_id,telegram_user_id,target_username,system_prompt,initial_prompt,status)
            VALUES($1,$2,$3::integer,$3::integer,'test_attention','','','active')""", session_id, ACCOUNT, dialog_id)

    async def message(self, message_id, tg_id, *, dialog_id=DIALOG, outgoing=False,
                      at=None, saved_at=NOW, deleted=False):
        await self.conn.execute("""INSERT INTO messages
            (id,dialog_id,telegram_message_id,is_outgoing,date,saved_at,deleted_at)
            VALUES($1,$2,$3,$4,$5,$6,$7)""", message_id, dialog_id, tg_id, outgoing,
            at or NOW-timedelta(minutes=10), saved_at, NOW if deleted else None)

    async def rows(self, after=0, limit=100):
        return await self.conn.fetch(_PAGE_SQL, after, ACCOUNT, limit)

    async def test_same_second_telegram_order_wins_over_import_insert_order(self):
        await self.message(260031, 100, outgoing=True)
        await self.message(260030, 101)
        row = (await self.rows())[0]
        self.assertEqual(row["inbound_message_id"], 260030)
        self.assertFalse(row["is_outgoing"])

    async def test_later_confirmed_outgoing_prevents_false_candidate(self):
        await self.message(260031, 100)
        await self.message(260030, 101, outgoing=True)
        row = (await self.rows())[0]
        self.assertTrue(row["is_outgoing"])
        self.assertEqual(classify_attention(row, None, now=NOW)["reason"], "no_unanswered_inbound")

    async def test_deleted_latest_does_not_hide_previous_input(self):
        await self.message(260031, 100)
        await self.message(260032, 101, outgoing=True, deleted=True)
        row = (await self.rows())[0]
        self.assertEqual(row["telegram_message_id"], 100)

    async def test_old_message_newly_saved_gets_grace(self):
        await self.message(260031, 100, at=NOW-timedelta(days=3))
        row = (await self.rows())[0]
        self.assertEqual(classify_attention(row, None, now=NOW)["reason"], "history_sync_grace")

    async def test_legacy_discovery_is_null_not_fabricated(self):
        await self.message(260031, 100, saved_at=None)
        row = (await self.rows())[0]
        self.assertIsNone(row["discovered_at"])

    async def test_paging_does_not_repeat_dialogs(self):
        await self.dialog(DIALOG+1, SESSION+1)
        first = await self.rows(limit=1)
        second = await self.rows(after=first[0]["dialog_id"], limit=1)
        self.assertEqual([first[0]["dialog_id"], second[0]["dialog_id"]], [DIALOG, DIALOG+1])
        self.assertEqual(await self.rows(after=second[0]["dialog_id"]), [])

    async def test_latest_session_and_batch_are_not_duplicated(self):
        await self.message(260031, 100)
        await self.conn.execute("UPDATE autochat_sessions SET status='stopped' WHERE id=$1", SESSION)
        await self.conn.execute("""INSERT INTO autochat_sessions
            (id,account_id,dialog_id,telegram_user_id,target_username,system_prompt,initial_prompt,status)
            VALUES($1,$2,$3::integer,$3::integer,'test_attention','','','active')""", SESSION+1, ACCOUNT, DIALOG)
        await self.conn.execute("""INSERT INTO autochat_reply_batches
            (session_id,dialog_id,account_id,inbound_message_id,phase)
            VALUES($1,$2,$3,$4,'sending')""", SESSION+1, DIALOG, ACCOUNT, 260031)
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], SESSION+1)
        self.assertEqual(classify_attention(rows[0], None, now=NOW)["reason"], "send_outcome_unknown")

    async def test_media_query_matches_undeleted_dialog_history(self):
        await self.message(260031, 100, saved_at=NOW-timedelta(minutes=10))
        await self.conn.execute("""INSERT INTO media(message_id,type,transcription_status)
            VALUES(260031,'voice','pending')""")
        row = (await self.rows())[0]
        self.assertTrue(row["pending_media"])
        item = classify_attention(row, {"registered": True, "worker_connected": True}, now=NOW)
        self.assertEqual(item["reason"], "media_overdue")

    async def test_partial_sent_batch_still_exposes_unknown_delivery(self):
        await self.message(260031, 100, saved_at=NOW-timedelta(minutes=10))
        await self.message(260032, 101, outgoing=True)
        await self.conn.execute("""INSERT INTO autochat_reply_batches
            (session_id,dialog_id,account_id,inbound_message_id,phase,next_index)
            VALUES($1,$2,$3,$4,'sending',1)""", SESSION, DIALOG, ACCOUNT, 260031)
        row = (await self.rows())[0]
        self.assertTrue(row["is_outgoing"])
        self.assertEqual(row["inbound_message_id"], 260031)
        self.assertEqual(classify_attention(row, None, now=NOW)["reason"], "send_outcome_unknown")


if __name__ == "__main__":
    unittest.main()
