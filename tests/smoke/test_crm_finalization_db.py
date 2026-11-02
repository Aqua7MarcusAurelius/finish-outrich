"""Real-SQL tests, opt-in ONLY against an explicit disposable *_test database.

TEST_CRM_DSN=... python -m unittest tests.smoke.test_crm_finalization_db
Every test rolls back its transaction; HTTP and Telegram are mocks.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import asyncpg

from core.events import EventType
from modules.partner_crm.errors import CrmHttpError
from modules.partner_crm.finalizer import PartnerCrmFinalizer
from modules.report_bot.service import ReportBotService

DSN = os.environ.get("TEST_CRM_DSN", "")
ID = 300001
EVENT = {"type": EventType.DIALOG_STATUS_CHANGED, "data": {"dialog_id": ID, "status_id": 6}}


@unittest.skipUnless(DSN, "requires explicit disposable TEST_CRM_DSN")
class CrmJournalDatabaseTests(unittest.IsolatedAsyncioTestCase):
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
        await self.conn.execute("""
            INSERT INTO accounts(id,name,phone,proxy_primary,proxy_fallback)
            VALUES ($1,'CRM_TEST','test_crm_300001','test://none','test://none')
        """, ID)
        await self.conn.execute("""
            INSERT INTO dialogs(id,account_id,telegram_user_id,username,from_crm,status_id,
                                partner_crm_terminal_reason)
            VALUES ($1,$1,$2,'crm_test_300001',TRUE,6,'no_response')
        """, ID, ID)
        await self.conn.execute("""
            UPDATE settings SET value='true' WHERE key='partner_crm.finalizer_retry_enabled';
            UPDATE settings SET value='100' WHERE key='partner_crm.retry_owner_id';
        """)
        self.service = SimpleNamespace(
            is_configured=lambda: True,
            push_lead_finalized=AsyncMock(),
            _cleanup_after_terminal=AsyncMock(),
            client=SimpleNamespace(get_lead_card=AsyncMock(return_value={
                "lead_status": "in_progress", "assigned_user_id": 100,
            })),
        )
        self.finalizer = PartnerCrmFinalizer(self.service)

    async def asyncTearDown(self):
        self.pool_patch.stop()
        await self.tx.rollback()
        await self.conn.close()

    async def job(self):
        return await self.conn.fetchrow("SELECT * FROM partner_crm_finalizations WHERE dialog_id=$1", ID)

    async def due(self):
        await self.conn.execute("""
            UPDATE partner_crm_finalizations SET next_attempt_at=NOW()-INTERVAL '1 minute'
            WHERE dialog_id=$1
        """, ID)

    async def seed_moderation(self):
        await self.conn.execute("""
            INSERT INTO report_moderation(dialog_id,username,account_id,family,chat_id,
                                          message_id,choice,push_after)
            VALUES ($1,'crm_test_300001',$1,'TEST',-300001,1,'take',NOW()-INTERVAL '1 hour')
        """, ID)

    async def test_duplicate_events_do_not_repeat_confirmed_delivery(self):
        await self.finalizer._handle(EVENT)
        await self.finalizer._handle(EVENT)
        self.service.push_lead_finalized.assert_awaited_once()
        job = await self.job()
        self.assertEqual(job["state"], "confirmed")
        self.assertEqual(job["confirmed_status"], "no_response")
        self.assertIsNone(await self.conn.fetchval(
            "SELECT partner_crm_terminal_reason FROM dialogs WHERE id=$1", ID,
        ))

    async def test_timeout_after_accepted_post_reconciles_on_new_instance(self):
        self.service.push_lead_finalized.side_effect = CrmHttpError("response lost")
        await self.finalizer._handle(EVENT)
        self.assertEqual((await self.job())["state"], "retry")
        self.service.client.get_lead_card.return_value = {"lead_status": "no_response", "assigned_user_id": 100}
        await self.due()
        await PartnerCrmFinalizer(self.service)._retry_due()
        self.service.push_lead_finalized.assert_awaited_once()
        self.service._cleanup_after_terminal.assert_awaited_once()
        self.assertEqual((await self.job())["state"], "confirmed")

    async def test_transient_failure_retries_only_owned_in_progress(self):
        self.service.push_lead_finalized.side_effect = [CrmHttpError("unavailable", status=503), None]
        await self.finalizer._handle(EVENT)
        await self.due()
        await self.finalizer._retry_due()
        self.assertEqual(self.service.push_lead_finalized.await_count, 2)
        self.service.client.get_lead_card.assert_awaited_once()
        self.assertEqual((await self.job())["state"], "confirmed")

    async def test_permanent_404_stops_automatic_retries(self):
        self.service.push_lead_finalized.side_effect = CrmHttpError("missing", status=404)
        await self.finalizer._handle(EVENT)
        await self.due()
        await self.finalizer._retry_due()
        self.service.push_lead_finalized.assert_awaited_once()
        self.assertEqual((await self.job())["state"], "review")

    async def test_read_failures_also_consume_bounded_attempts(self):
        self.service.push_lead_finalized.side_effect = CrmHttpError("response lost")
        self.service.client.get_lead_card.side_effect = CrmHttpError("unavailable", status=503)
        await self.finalizer._handle(EVENT)
        for _ in range(4):
            await self.due()
            await self.finalizer._retry_due()
        self.assertEqual((await self.job())["state"], "review")
        self.assertEqual((await self.job())["attempts"], 3)
        self.assertEqual(self.service.client.get_lead_card.await_count, 2)
        self.service.push_lead_finalized.assert_awaited_once()

    async def test_new_reason_during_http_is_not_cleared(self):
        async def change_decision(**kwargs):
            await self.conn.execute("""
                UPDATE dialogs SET status_id=1, partner_crm_terminal_reason='invalid'
                WHERE id=$1
            """, ID)
        self.service.push_lead_finalized.side_effect = change_decision
        await self.finalizer._handle(EVENT)
        self.assertEqual((await self.job())["state"], "superseded")
        self.assertEqual(await self.conn.fetchval(
            "SELECT partner_crm_terminal_reason FROM dialogs WHERE id=$1", ID,
        ), "invalid")

    async def test_intermediate_status_supersedes_retry(self):
        self.service.push_lead_finalized.side_effect = CrmHttpError("offline")
        await self.finalizer._handle(EVENT)
        await self.conn.execute("UPDATE dialogs SET status_id=3 WHERE id=$1", ID)
        await self.due()
        await self.finalizer._retry_due()
        self.assertEqual((await self.job())["state"], "superseded")
        self.service.push_lead_finalized.assert_awaited_once()

    async def test_changed_owner_or_terminal_status_is_not_overwritten(self):
        for card in ({"lead_status": "in_progress", "assigned_user_id": 999},
                     {"lead_status": "approved", "assigned_user_id": 100}):
            with self.subTest(card=card):
                # Test-specific journal reset, not any production history scan.
                await self.conn.execute("DELETE FROM partner_crm_finalizations WHERE dialog_id=$1", ID)
                self.service.push_lead_finalized.reset_mock()
                self.service.push_lead_finalized.side_effect = CrmHttpError("response lost")
                self.service.client.get_lead_card.return_value = card
                await self.finalizer._handle(EVENT)
                await self.due()
                await self.finalizer._retry_due()
                self.service.push_lead_finalized.assert_awaited_once()
                self.assertEqual((await self.job())["state"], "review")

    async def test_pending_moderation_blocks_terminal_finalizer(self):
        await self.seed_moderation()
        await self.finalizer._handle(EVENT)
        self.service.push_lead_finalized.assert_not_awaited()
        self.assertIsNone(await self.job())

    async def test_retry_disabled_keeps_pending_receipt_without_write(self):
        self.service.push_lead_finalized.side_effect = CrmHttpError("offline")
        await self.finalizer._handle(EVENT)
        await self.conn.execute("UPDATE settings SET value='false' WHERE key='partner_crm.finalizer_retry_enabled'")
        await self.due()
        await self.finalizer._retry_due()
        self.service.push_lead_finalized.assert_awaited_once()
        self.assertEqual((await self.job())["state"], "retry")

    async def test_callback_waits_for_inflight_finalization_and_cannot_replace_choice(self):
        await self.seed_moderation()
        started, release = asyncio.Event(), asyncio.Event()
        async def push(**kwargs):
            started.set()
            await release.wait()
            return "transferred"
        crm = SimpleNamespace(push_lead_moderated=AsyncMock(side_effect=push))
        bot = ReportBotService(crm)
        bot._families = {"TEST": {"l2_login": "l2_test"}}
        bot._set_final_bubble = AsyncMock()
        bot._seal_message = AsyncMock()
        event = SimpleNamespace(data=f"m:invalid:{ID}".encode(), sender_id=123,
            get_sender=AsyncMock(return_value=SimpleNamespace(username="tester")),
            answer=AsyncMock(), edit=AsyncMock())
        inflight = asyncio.create_task(bot._finalize_one({"dialog_id": ID, "choice": "invalid"}))
        await started.wait()
        callback = asyncio.create_task(bot._on_callback(event))
        await asyncio.sleep(0)
        self.assertFalse(callback.done())
        release.set()
        await asyncio.gather(inflight, callback)
        row = await self.conn.fetchrow("SELECT choice,pushed_status FROM report_moderation WHERE dialog_id=$1", ID)
        self.assertEqual(row["choice"], "take")
        self.assertEqual(row["pushed_status"], "transferred")
        self.assertTrue(event.answer.await_args.kwargs["alert"])
        event.edit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
