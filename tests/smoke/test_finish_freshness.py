"""Finish/evaluator race regressions, using only the explicit isolated PostgreSQL."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
from core import db, bus
from modules.autochat.service import AutoChatService
from modules.lead_evaluator import service as evaluator
from modules.lead_evaluator.parser import EvaluationResult

ACCOUNT, DIALOG, SESSION, INPUT = 270001, 270010, 270020, 270030
NOW = datetime(2026, 10, 27, 16, tzinfo=timezone.utc)


@unittest.skipUnless(os.getenv("OUTRICH_ISOLATED_SQL_TESTS") == "1", "isolated DB opt-in required")
class FinishFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = await asyncpg.connect(host="outrich-recovery-test-db", port=5432,
            user="outrich_test", password="isolated_test_only", database="outrich_test")
        self.addAsyncCleanup(self.conn.close)
        self.tx = self.conn.transaction()
        await self.tx.start()
        async def rollback():
            await self.tx.rollback()
        self.addAsyncCleanup(rollback)
        await self.conn.execute("""INSERT INTO accounts(id,name,phone,proxy_primary,proxy_fallback)
            VALUES($1,'TEST_FINISH','+000270001','test','test')""", ACCOUNT)
        await self.conn.execute("""INSERT INTO dialogs(id,account_id,telegram_user_id)
            VALUES($1::integer,$2,$1::integer)""", DIALOG, ACCOUNT)
        await self.conn.execute("""INSERT INTO autochat_sessions
            (id,account_id,dialog_id,telegram_user_id,target_username,system_prompt,initial_prompt,status)
            VALUES($1,$2,$3::integer,$3::integer,'test_finish','','','active')""", SESSION, ACCOUNT, DIALOG)
        await self.incoming(INPUT, 100)
        self.batch_id = await self.conn.fetchval("""INSERT INTO autochat_reply_batches
            (session_id,dialog_id,account_id,inbound_message_id,phase,finish_requested)
            VALUES($1,$2,$3,$4,'complete',true) RETURNING id""", SESSION, DIALOG, ACCOUNT, INPUT)
        @asynccontextmanager
        async def acquire():
            yield self.conn
        self.pool = MagicMock()
        self.pool.acquire = acquire
        self.db_patch = patch.object(db, "get_pool", return_value=self.pool)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.bus_patch = patch.object(bus, "publish", AsyncMock(return_value={"id": "test"}))
        self.publish = self.bus_patch.start()
        self.addCleanup(self.bus_patch.stop)

    async def incoming(self, message_id, tg_id):
        await self.conn.execute("""INSERT INTO messages(id,dialog_id,telegram_message_id,is_outgoing,date)
            VALUES($1,$2,$3,false,$4)""", message_id, DIALOG, tg_id, NOW)

    def service(self):
        service = AutoChatService(get_wrapper=lambda _: None)
        session = MagicMock()
        session.stop = AsyncMock()
        service._sessions_by_id[SESSION] = session
        service._by_dialog[(ACCOUNT, DIALOG)] = SESSION
        service._by_tg_user[(ACCOUNT, DIALOG)] = SESSION
        return service, session

    async def test_new_inbound_prevents_old_finish_from_stopping_session(self):
        service, session = self.service()
        await self.incoming(INPUT+1, 101)
        result = await service.stop_session(SESSION, expected_batch_id=self.batch_id)
        self.assertIsNone(result)
        self.assertIs(service._sessions_by_id[SESSION], session)
        session.stop.assert_not_awaited()
        self.assertEqual(await self.conn.fetchval("SELECT status FROM autochat_sessions WHERE id=$1", SESSION), "active")
        self.publish.assert_not_awaited()

    async def test_guarded_stop_commits_db_state_before_removing_runtime(self):
        service, session = self.service()
        async def stopped(**kwargs):
            self.assertEqual(await self.conn.fetchval("SELECT status FROM autochat_sessions WHERE id=$1", SESSION), "stopped")
            self.assertNotIn(SESSION, service._sessions_by_id)
        session.stop.side_effect = stopped
        result = await service.stop_session(SESSION, expected_batch_id=self.batch_id)
        self.assertEqual(result["status"], "stopped")
        session.stop.assert_awaited_once_with(reason="llm_finish")

    async def test_newer_batch_prevents_old_finish_even_with_same_input(self):
        service, session = self.service()
        await self.conn.execute("""INSERT INTO autochat_reply_batches
            (session_id,dialog_id,account_id,inbound_message_id,phase)
            VALUES($1,$2,$3,$4,'generating')""", SESSION, DIALOG, ACCOUNT, INPUT)
        self.assertIsNone(await service.stop_session(SESSION, expected_batch_id=self.batch_id))
        session.stop.assert_not_awaited()

    async def test_confirmed_segments_must_be_in_history_before_evaluation(self):
        await self.conn.execute("UPDATE autochat_reply_batches SET segments=$2::jsonb WHERE id=$1",
            self.batch_id, json.dumps([{"text": "test", "telegram_message_id": 101}]))
        with self.assertRaises(evaluator._FinishHistoryPending):
            await evaluator._finish_guard(self.conn, DIALOG, self.batch_id, INPUT)

    async def evaluate(self, effect=None):
        svc = evaluator.LeadEvaluatorService()
        async def llm(*args, **kwargs):
            if effect:
                await effect()
            return EvaluationResult(verdict="success", parse_succeeded=True), "<verdict>success</verdict>"
        svc._call_llm_with_retries = AsyncMock(side_effect=llm)
        with patch.object(evaluator, "build_evaluator_messages", AsyncMock(return_value=[])):
            result = await svc.evaluate(DIALOG, source="autochat.finished_by_llm",
                expected_batch_id=self.batch_id, expected_input_message_id=INPUT)
        return result, svc

    async def test_input_arriving_during_llm_does_not_commit_old_evaluation(self):
        async def incoming():
            await self.incoming(INPUT+1, 101)
        result, _ = await self.evaluate(incoming)
        self.assertTrue(result["skipped"])
        self.assertIsNone(await self.conn.fetchval("SELECT verdict FROM lead_evaluations WHERE dialog_id=$1", DIALOG))
        self.assertIsNone(await self.conn.fetchval("SELECT status_id FROM dialogs WHERE id=$1", DIALOG))

    async def test_operator_decision_during_llm_is_preserved(self):
        terminal = await self.conn.fetchval("SELECT id FROM dialog_statuses WHERE closes_dialog LIMIT 1")
        async def decision():
            await self.conn.execute("UPDATE dialogs SET status_id=$2,status_source='manual',partner_crm_terminal_reason='invalid' WHERE id=$1", DIALOG, terminal)
        result, _ = await self.evaluate(decision)
        self.assertTrue(result["skipped"])
        row = await self.conn.fetchrow("SELECT status_id,partner_crm_terminal_reason FROM dialogs WHERE id=$1", DIALOG)
        self.assertEqual(row["status_id"], terminal)
        self.assertEqual(row["partner_crm_terminal_reason"], "invalid")

    async def test_moderation_during_llm_is_preserved_even_before_status_update(self):
        async def moderation():
            await self.conn.execute("""INSERT INTO report_moderation(dialog_id,username,chat_id,message_id,choice)
                VALUES($1,'test_finish',-1,1,'take')""", DIALOG)
        result, _ = await self.evaluate(moderation)
        self.assertTrue(result["skipped"])
        self.assertIsNone(await self.conn.fetchval("SELECT verdict FROM lead_evaluations WHERE dialog_id=$1", DIALOG))
        # report_moderation has no FK; rollback still removes the fixture.

    async def test_existing_terminal_is_skipped_before_llm(self):
        await self.conn.execute("UPDATE dialogs SET status_id=(SELECT id FROM dialog_statuses WHERE closes_dialog LIMIT 1) WHERE id=$1", DIALOG)
        result, svc = await self.evaluate()
        self.assertTrue(result["skipped"])
        svc._call_llm_with_retries.assert_not_awaited()

    async def test_existing_evaluation_is_preserved_even_if_status_is_missing(self):
        await self.conn.execute("INSERT INTO lead_evaluations(dialog_id,verdict) VALUES($1,'success')", DIALOG)
        result, svc = await self.evaluate()
        self.assertTrue(result["skipped"])
        svc._call_llm_with_retries.assert_not_awaited()

    async def test_current_finish_commits_evaluation_status_and_reason(self):
        result, _ = await self.evaluate()
        self.assertEqual(result["verdict"], "success")
        row = await self.conn.fetchrow("""SELECT s.label,d.status_source,d.partner_crm_terminal_reason
            FROM dialogs d JOIN dialog_statuses s ON s.id=d.status_id WHERE d.id=$1""", DIALOG)
        self.assertEqual(row["label"], "Модерация")
        self.assertEqual(row["status_source"], "event:autochat.finished_by_llm")
        self.assertIsNone(row["partner_crm_terminal_reason"])


class EventContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_event_forwards_guard_metadata(self):
        svc = evaluator.LeadEvaluatorService()
        svc.evaluate = AsyncMock()
        await svc._on_finished_by_llm({"data": {"dialog_id": 10, "reply_batch_id": 20,
                                                 "input_message_id": 30}})
        svc.evaluate.assert_awaited_once_with(10, source="autochat.finished_by_llm",
            expected_batch_id=20, expected_input_message_id=30)


if __name__ == "__main__":
    unittest.main()
