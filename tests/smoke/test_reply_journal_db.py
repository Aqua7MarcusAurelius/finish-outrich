"""Reply checkpoint real-SQL tests, opt-in disposable *_test database only.

Every fixture is transaction-scoped; all transport/generation is absent.
Run with TEST_CRM_DSN and `python -m unittest tests.smoke.test_reply_journal_db`.
"""
from __future__ import annotations

import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import asyncpg
from modules.autochat import reply_journal as journal

DSN = os.environ.get("TEST_CRM_DSN", "")
ID = 400001
BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


@unittest.skipUnless(DSN, "requires explicit disposable TEST_CRM_DSN")
class ReplyJournalDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not urlparse(DSN).path.endswith("_test"):
            raise RuntimeError("Refusing database name not ending in _test")
        self.conn = await asyncpg.connect(DSN)
        self.tx = self.conn.transaction()
        await self.tx.start()
        async def cleanup_connection():
            try:
                await self.tx.rollback()
            finally:
                await self.conn.close()
        self.addAsyncCleanup(cleanup_connection)
        @asynccontextmanager
        async def acquire():
            yield self.conn
        pool_patch = patch("core.db.get_pool", return_value=SimpleNamespace(acquire=acquire))
        pool_patch.start()
        self.addCleanup(pool_patch.stop)
        await self.conn.execute("""INSERT INTO accounts(id,name,phone,proxy_primary,proxy_fallback)
            VALUES ($1,'REPLY_TEST','test_reply_400001','test://none','test://none')""", ID)
        await self.conn.execute("""INSERT INTO dialogs(id,account_id,telegram_user_id,username,status_id)
            VALUES ($1,$1,$2,'reply_test_400001',3)""", ID, ID)
        await self.conn.execute("""INSERT INTO autochat_sessions
            (id,account_id,dialog_id,telegram_user_id,target_username,system_prompt,initial_prompt,status)
            VALUES($1,$1,$1,$2,'reply_test_400001','test','test','active')""", ID, ID)
        await self.message(ID+1, 100, incoming=True, second=0)

    async def message(self, row_id, tg_id, *, incoming, second):
        await self.conn.execute("""INSERT INTO messages(id,dialog_id,telegram_message_id,is_outgoing,date,text)
            VALUES($1,$2,$3,$4,$5,$6)""", row_id, ID, tg_id, not incoming,
            BASE_TIME+timedelta(seconds=second), f'test message {tg_id}')

    async def phase(self, batch):
        return await self.conn.fetchval("SELECT phase FROM autochat_reply_batches WHERE id=$1", batch['id'])

    async def ready(self, *, texts=None, finish=False):
        batch = await journal.begin(ID, ID, ID)
        self.assertIsNotNone(batch)
        result = await journal.store_result(batch, texts if texts is not None else ['reply'], finish, 'test-parent')
        self.assertIsNotNone(result)
        return result

    async def test_intent_ready_sending_confirmed_and_partial_data_survive(self):
        batch = await journal.begin(ID, ID, ID)
        self.assertEqual(await self.phase(batch), 'generating')
        batch = await journal.store_result(batch, ['one', 'two'], False, 'parent-1')
        self.assertEqual(batch['segments'], [{'text':'one'}, {'text':'two'}])
        self.assertTrue(await journal.before_send(batch['id'], ID, 0))
        self.assertEqual(await self.phase(batch), 'sending')
        await journal.confirm_send(batch['id'], ID, 0, 101)
        restored = await journal.pending(ID)
        self.assertEqual(restored['phase'], 'ready')
        self.assertEqual(restored['next_index'], 1)
        self.assertEqual(restored['segments'], [{'text':'one','telegram_message_id':101}, {'text':'two'}])
        await self.message(ID+2, 101, incoming=False, second=1)
        self.assertTrue(await journal.before_send(batch['id'], ID, 1))
        await journal.confirm_send(batch['id'], ID, 1, 102)
        self.assertEqual(await self.phase(batch), 'complete')
        self.assertIsNone(await journal.pending(ID))

    async def test_duplicate_begin_and_generation_result_are_rejected(self):
        batch = await journal.begin(ID, ID, ID)
        self.assertIsNone(await journal.begin(ID, ID, ID))
        result = await journal.store_result(batch, ['first'], False, 'p')
        self.assertIsNotNone(result)
        self.assertIsNone(await journal.store_result(batch, ['duplicate'], False, 'p'))
        self.assertEqual((await journal.pending(ID))['segments'], [{'text':'first'}])

    async def test_unknown_rpc_blocks_new_input_and_manual_force(self):
        batch = await self.ready()
        self.assertTrue(await journal.before_send(batch['id'], ID, 0))
        await self.message(ID+2, 101, incoming=True, second=1)
        self.assertIsNone(await journal.begin(ID, ID, ID))
        self.assertIsNone(await journal.begin(ID, ID, ID, manual=True))
        await journal.review(batch['id'], ID, 'delivery_unknown')
        self.assertIsNone(await journal.begin(ID, ID, ID))
        self.assertEqual(await self.phase(batch), 'review')

    async def test_new_input_supersedes_unstarted_generation(self):
        old = await journal.begin(ID, ID, ID)
        await self.message(ID+2, 101, incoming=True, second=1)
        self.assertIsNone(await journal.store_result(old, ['stale reply'], False, 'p'))
        self.assertEqual(await self.phase(old), 'superseded')
        new = await journal.begin(ID, ID, ID)
        self.assertEqual(new['inbound_message_id'], ID+2)

    async def test_new_input_supersedes_ready_unsent_parts(self):
        old = await self.ready(texts=['one','two'])
        await self.message(ID+2, 101, incoming=True, second=1)
        self.assertFalse(await journal.before_send(old['id'], ID, 0))
        self.assertEqual(await self.phase(old), 'superseded')
        new = await journal.begin(ID, ID, ID)
        self.assertEqual(new['inbound_message_id'], ID+2)

    async def test_new_input_during_rpc_is_not_mistaken_for_answered_input(self):
        batch = await self.ready()
        self.assertTrue(await journal.before_send(batch['id'], ID, 0))
        await self.message(ID+2, 101, incoming=True, second=1)
        await journal.confirm_send(batch['id'], ID, 0, 102)
        pending = await journal.pending(ID)
        self.assertEqual(pending['inbound_message_id'], ID+2)
        self.assertEqual(pending['phase'], 'generating')
        await self.message(ID+3, 102, incoming=False, second=2)
        new = await journal.begin(ID, ID, ID, reuse_intent=True)
        self.assertIsNotNone(new)
        self.assertEqual(new['id'], pending['id'])
        self.assertEqual(new['inbound_message_id'], ID+2)
        self.assertIsNotNone(await journal.store_result(new, ['answer to newer input'], False, 'p-new'))

    async def test_completed_input_cannot_generate_again_before_history_save(self):
        batch = await self.ready()
        self.assertTrue(await journal.before_send(batch['id'], ID, 0))
        await journal.confirm_send(batch['id'], ID, 0, 101)
        # History writer has not yet saved outgoing 101, but delivery is known.
        self.assertIsNone(await journal.begin(ID, ID, ID))

    async def test_new_input_waits_for_confirmed_output_to_enter_context(self):
        batch = await self.ready()
        self.assertTrue(await journal.before_send(batch['id'], ID, 0))
        await self.message(ID+2, 101, incoming=True, second=1)
        await journal.confirm_send(batch['id'], ID, 0, 102)
        # New generation must see our actual previous answer in its history.
        pending = await journal.pending(ID)
        self.assertEqual(pending['inbound_message_id'], ID+2)
        self.assertIsNone(await journal.begin(ID, ID, ID, reuse_intent=True))
        await self.message(ID+3, 102, incoming=False, second=2)
        new = await journal.begin(ID, ID, ID, reuse_intent=True)
        self.assertEqual(new['inbound_message_id'], ID+2)
        self.assertEqual(new['id'], pending['id'])

    async def test_new_input_intent_survives_ack_before_outgoing_publish(self):
        batch = await self.ready(texts=['first', 'unsent tail'], finish=True)
        self.assertTrue(await journal.before_send(batch['id'], ID, 0))
        await self.message(ID+2, 101, incoming=True, second=1)
        await journal.confirm_send(batch['id'], ID, 0, 102)
        # Simulate restart at the very next instruction: no message.received,
        # no planner bump and no saved outgoing. All evidence is in PostgreSQL.
        self.assertEqual(await self.phase(batch), 'superseded')
        restored = await journal.pending(ID)
        self.assertEqual(restored['phase'], 'generating')
        self.assertEqual(restored['inbound_message_id'], ID+2)
        self.assertNotEqual(restored['id'], batch['id'])
        old = journal.unpack(await self.conn.fetchrow(
            'SELECT * FROM autochat_reply_batches WHERE id=$1', batch['id']))
        self.assertEqual(old['segments'][0]['telegram_message_id'], 102)
        self.assertEqual(old['segments'][1], {'text':'unsent tail'})
        self.assertIsNone(await journal.begin(ID, ID, ID, reuse_intent=True))
        self.assertFalse(await journal.start_finish(batch['id'], ID))
        await self.message(ID+3, 102, incoming=False, second=2)
        claimed = await journal.begin(ID, ID, ID, reuse_intent=True)
        self.assertEqual(claimed['id'], restored['id'])
        self.assertIsNotNone(await journal.store_result(claimed, ['new reply'], False, 'new'))

    async def test_partial_response_is_preserved_when_new_input_supersedes_tail(self):
        batch = await self.ready(texts=['one','two'])
        self.assertTrue(await journal.before_send(batch['id'], ID, 0))
        await journal.confirm_send(batch['id'], ID, 0, 101)
        await self.message(ID+2, 101, incoming=False, second=1)
        await self.message(ID+3, 102, incoming=True, second=2)
        self.assertFalse(await journal.before_send(batch['id'], ID, 1))
        saved = journal.unpack(await self.conn.fetchrow('SELECT * FROM autochat_reply_batches WHERE id=$1', batch['id']))
        self.assertEqual(saved['phase'], 'superseded')
        self.assertEqual(saved['segments'][0]['telegram_message_id'], 101)
        new = await journal.begin(ID, ID, ID)
        self.assertEqual(new['inbound_message_id'], ID+3)

    async def test_terminal_evaluation_and_moderation_are_protected(self):
        await self.conn.execute('UPDATE dialogs SET status_id=6 WHERE id=$1', ID)
        self.assertIsNone(await journal.begin(ID, ID, ID))
        await self.conn.execute('UPDATE dialogs SET status_id=3 WHERE id=$1', ID)
        await self.conn.execute("INSERT INTO lead_evaluations(dialog_id,verdict) VALUES($1,'success')", ID)
        self.assertIsNone(await journal.begin(ID, ID, ID))
        await self.conn.execute('DELETE FROM lead_evaluations WHERE dialog_id=$1', ID)
        await self.conn.execute("""INSERT INTO report_moderation
            (dialog_id,username,account_id,chat_id,message_id)
            VALUES($1,'reply_test_400001',$1,-400001,1)""", ID)
        self.assertIsNone(await journal.begin(ID, ID, ID))

    async def test_stop_after_generation_prevents_send(self):
        batch = await self.ready()
        await self.conn.execute("UPDATE autochat_sessions SET status='stopped' WHERE id=$1", ID)
        self.assertFalse(await journal.before_send(batch['id'], ID, 0))
        self.assertEqual(await self.phase(batch), 'superseded')
        self.assertIsNone(await journal.begin(ID, ID, ID, manual=True))

    async def test_finish_marker_claim_cannot_publish_twice(self):
        batch = await self.ready(texts=[], finish=True)
        self.assertTrue(await journal.start_finish(batch['id'], ID))
        self.assertFalse(await journal.start_finish(batch['id'], ID))
        await journal.finish_confirmed(batch['id'], ID)
        self.assertEqual(await self.phase(batch), 'complete')
        self.assertFalse(await journal.start_finish(batch['id'], ID))
        # No outgoing message exists for a finish-only result.
        self.assertIsNone(await journal.begin(ID, ID, ID))

    async def test_finish_waits_for_confirmed_segments_in_evaluator_history(self):
        batch = await self.ready(texts=['last reply'], finish=True)
        self.assertTrue(await journal.before_send(batch['id'], ID, 0))
        await journal.confirm_send(batch['id'], ID, 0, 101)
        # Evaluation/report rendering read messages, not the checkpoint JSON.
        self.assertFalse(await journal.start_finish(batch['id'], ID))
        self.assertEqual(await self.phase(batch), 'ready')
        await self.message(ID+2, 101, incoming=False, second=1)
        self.assertTrue(await journal.start_finish(batch['id'], ID))

    async def test_new_input_during_finish_publication_retains_pending_intent(self):
        batch = await self.ready(texts=[], finish=True)
        self.assertTrue(await journal.start_finish(batch['id'], ID))
        await self.message(ID+2, 101, incoming=True, second=1)
        await journal.finish_confirmed(batch['id'], ID)
        # A stale finish callback must not be our only protection: restart
        # before the planner runs must still find the newer durable input.
        self.assertEqual(await self.phase(batch), 'superseded')
        pending = await journal.pending(ID)
        self.assertIsNotNone(pending)
        self.assertEqual(pending['inbound_message_id'], ID+2)
        self.assertEqual(pending['phase'], 'generating')

    async def test_missing_telegram_delivery_id_requires_review(self):
        batch = await self.ready()
        self.assertTrue(await journal.before_send(batch['id'], ID, 0))
        with self.assertRaises(RuntimeError):
            await journal.confirm_send(batch['id'], ID, 0, None)
        self.assertEqual(await self.phase(batch), 'review')
        self.assertIsNone(await journal.begin(ID, ID, ID))


if __name__ == '__main__':
    unittest.main()
