"""Offline safety tests: python -m unittest tests.smoke.test_crm_finalization_safety.

No database/network calls. Integration coverage lives in the explicit disposable
PostgreSQL suite; these tests exercise policy, failures and coroutine ordering.
"""
from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from modules.partner_crm.errors import CrmHttpError, InvalidStatus
from modules.partner_crm.finalizer import reconciliation_action, retryable
from modules.partner_crm.guard import dialog_guard
from modules.partner_crm.service import PartnerCrmService
from modules.report_bot.service import ReportBotService


class PolicyTests(unittest.TestCase):
    def test_only_transient_failures_are_retried(self):
        for status in (None, 429, 500, 503, 599):
            self.assertTrue(retryable(CrmHttpError("safe", status=status)))
        for status in (400, 401, 403, 404, 409, 422):
            self.assertFalse(retryable(CrmHttpError("safe", status=status)))
        self.assertFalse(retryable(InvalidStatus("bad")))

    def test_matching_remote_status_confirms_without_mutation(self):
        self.assertEqual(reconciliation_action({"lead_status": "invalid"}, "invalid", None), "confirmed")

    def test_only_owned_in_progress_card_can_be_retried(self):
        self.assertEqual(reconciliation_action(
            {"lead_status": "in_progress", "assigned_user_id": 100}, "invalid", 100,
        ), "send")
        for owner in (None, 99):
            self.assertNotEqual(reconciliation_action(
                {"lead_status": "in_progress", "assigned_user_id": 100}, "invalid", owner,
            ), "send")
        for status in (None, "approved", "transferred", "ready_for_transfer", "no_response", "new"):
            self.assertNotEqual(reconciliation_action(
                {"lead_status": status, "assigned_user_id": 100}, "invalid", 100,
            ), "send")


class ModerationSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_failure_never_triggers_blind_admin_write(self):
        client = MagicMock()
        client.get_lead_card = AsyncMock(side_effect=CrmHttpError("offline", status=503))
        client.set_lead_status_admin = AsyncMock()
        svc = PartnerCrmService(client)
        svc.push_lead_finalized = AsyncMock(side_effect=CrmHttpError("locked", status=403))
        with self.assertRaises(CrmHttpError):
            await svc._push_moderated_terminal(account_id=1, username="test", terminal="invalid")
        client.set_lead_status_admin.assert_not_awaited()

    async def test_empty_card_never_triggers_admin_write(self):
        client = MagicMock()
        client.get_lead_card = AsyncMock(return_value={})
        client.set_lead_status_admin = AsyncMock()
        svc = PartnerCrmService(client)
        svc.push_lead_finalized = AsyncMock(side_effect=CrmHttpError("locked", status=403))
        with self.assertRaises(CrmHttpError):
            await svc._push_moderated_terminal(account_id=1, username="test", terminal="invalid")
        client.set_lead_status_admin.assert_not_awaited()

    async def test_assignment_transient_or_unknown_failure_preserves_take(self):
        for status in (None, 429, 500, 503, 404, 403, 400):
            client = MagicMock()
            client.assign_to_l2 = AsyncMock(side_effect=CrmHttpError("failed", status=status))
            svc = PartnerCrmService(client)
            svc._push_moderated_terminal = AsyncMock()
            with self.assertRaises(CrmHttpError):
                await svc.push_lead_moderated(account_id=1, username="test", choice="take", l2_login="l2_test")
            svc._push_moderated_terminal.assert_not_awaited()

    async def test_explicit_missing_recipient_keeps_existing_fallback(self):
        client = MagicMock()
        client.assign_to_l2 = AsyncMock(side_effect=CrmHttpError(
            "missing", status=400, body='{"error":"l2_not_found"}',
        ))
        svc = PartnerCrmService(client)
        svc._push_moderated_terminal = AsyncMock(return_value="ready_for_transfer")
        result = await svc.push_lead_moderated(account_id=1, username="test", choice="take", l2_login="l2_test")
        self.assertEqual(result, "ready_for_transfer")
        svc._push_moderated_terminal.assert_awaited_once()

    async def test_l2_decision_is_preserved(self):
        for status in ("approved", "transferred"):
            client = MagicMock()
            client.get_lead_card = AsyncMock(return_value={"lead_status": status})
            client.set_lead_status_admin = AsyncMock()
            svc = PartnerCrmService(client)
            svc.push_lead_finalized = AsyncMock(side_effect=CrmHttpError("locked", status=403))
            svc._cleanup_after_terminal = AsyncMock()
            self.assertEqual(await svc._push_moderated_terminal(
                account_id=1, username="test", terminal="invalid",
            ), status)
            client.set_lead_status_admin.assert_not_awaited()

    async def test_ticker_uses_current_choice_not_stale_due_row(self):
        current = {"dialog_id": 991, "choice": "take", "family": "TEST"}
        conn = SimpleNamespace(fetchrow=AsyncMock(return_value=current))
        @asynccontextmanager
        async def acquire():
            yield conn
        bot = ReportBotService(MagicMock())
        bot._finalize_current = AsyncMock()
        with patch("modules.report_bot.service.db.get_pool", return_value=SimpleNamespace(acquire=acquire)):
            await bot._finalize_one({"dialog_id": 991, "choice": "invalid"})
        bot._finalize_current.assert_awaited_once_with(current)

    async def test_choice_changed_while_waiting_prevents_stale_push(self):
        # Callback owns the guard. While ticker waits, it moves the deadline.
        conn = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
        @asynccontextmanager
        async def acquire():
            yield conn
        bot = ReportBotService(MagicMock())
        bot._finalize_current = AsyncMock()
        with patch("modules.report_bot.service.db.get_pool", return_value=SimpleNamespace(acquire=acquire)):
            async with dialog_guard(992):
                pending = asyncio.create_task(bot._finalize_one({"dialog_id": 992, "choice": "invalid"}))
                await asyncio.sleep(0)
                conn.fetchrow.assert_not_awaited()
            await pending
        bot._finalize_current.assert_not_awaited()

    async def test_same_dialogue_serializes_but_other_dialogue_does_not(self):
        entered = asyncio.Event()
        async def contender():
            async with dialog_guard(993):
                entered.set()
        async with dialog_guard(993):
            task = asyncio.create_task(contender())
            await asyncio.sleep(0)
            self.assertFalse(entered.is_set())
            async with dialog_guard(994):
                self.assertFalse(entered.is_set())
        await task
        self.assertTrue(entered.is_set())


if __name__ == "__main__":
    unittest.main()
