"""Read-only diagnostics tests. Run: python -m unittest tests.smoke.test_autochat_attention."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from modules.autochat import attention

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)


def candidate(**overrides):
    row = {
        "account_id": 24, "dialog_id": 101, "session_id": 201,
        "session_status": "active", "inbound_message_id": 301,
        "telegram_message_id": 1500, "is_outgoing": False,
        "inbound_at": NOW - timedelta(minutes=20),
        "discovered_at": NOW - timedelta(minutes=20),
        "closes_dialog": False, "has_moderation": False,
    }
    row.update(overrides)
    return row


def live(**overrides):
    state = {"registered": True, "worker_connected": True, "sending": False,
             "generating": False, "queued_segments": 0}
    state.update(overrides)
    return state


class ClassifierTests(unittest.TestCase):
    def check_reason(self, reason, row=None, state=None, *, known=True, review=True):
        item = attention.classify_attention(row or candidate(),
                (state or live()) if known else None, now=NOW)
        self.assertEqual(item["reason"], reason)
        self.assertEqual(item["needs_review"], review)
        return item

    def test_missing_snapshot_never_means_idle(self):
        item = self.check_reason("live_state_unknown", known=False)
        self.assertFalse(item["live_state_known"])

    def test_dequeued_inflight_segment_is_not_a_missed_response(self):
        self.check_reason("delivery_pending", state=live(sending=True, queued_segments=0,
                          phase_started_at=NOW - timedelta(seconds=10)), review=False)

    def test_generation_and_media_have_finite_grace(self):
        for flag, reason in [("generating", "generation"), ("waiting_media", "media")]:
            with self.subTest(flag=flag):
                self.check_reason(reason + "_pending", state=live(**{flag: True,
                    "phase_started_at": NOW - timedelta(seconds=10)}), review=False)
                self.check_reason(reason + "_overdue", state=live(**{flag: True,
                    "phase_started_at": NOW - timedelta(hours=2)}))

    def test_newly_imported_old_message_gets_discovery_grace(self):
        item = self.check_reason("history_sync_grace", candidate(
            inbound_at=NOW-timedelta(days=3), discovered_at=NOW-timedelta(seconds=10)),
            review=False)
        self.assertTrue(item["outside_legacy_recovery_window"])

    def test_unknown_legacy_discovery_is_not_guessed_from_activity(self):
        self.check_reason("discovery_time_unknown", candidate(discovered_at=None,
            last_our_activity_at=NOW))

    def test_live_discovery_must_belong_to_exact_message(self):
        row = candidate(discovered_at=None)
        self.check_reason("discovery_time_unknown", row,
            live(last_inbound_message_id=999, last_inbound_seen_at=NOW))
        self.check_reason("history_sync_grace", row,
            live(last_inbound_message_id=301, last_inbound_seen_at=NOW), review=False)

    def test_activity_at_enqueue_is_not_delivery(self):
        self.check_reason("processing_unconfirmed", candidate(last_our_activity_at=NOW))

    def test_stopped_failed_paused_are_review_only(self):
        for status in ["stopped", "failed", "paused"]:
            self.check_reason("session_"+status, candidate(session_status=status))

    def test_take_invalid_and_moderation_are_not_reopened(self):
        self.check_reason("moderated_followup", candidate(has_moderation=True))
        self.check_reason("terminal_dialog_review", candidate(closes_dialog=True))

    def test_evaluated_finish_without_text_reply_is_processed(self):
        self.check_reason("completed_by_evaluation", candidate(
            session_status="stopped", closes_dialog=True, evaluated_at=NOW,
            evaluation_parse_succeeded=True, evaluation_verdict="archive"), review=False)

    def test_evaluation_before_latest_input_does_not_hide_followup(self):
        self.check_reason("moderated_followup", candidate(has_moderation=True,
            evaluated_at=NOW-timedelta(days=1), evaluation_parse_succeeded=True,
            evaluation_verdict="success"))

    def test_evaluation_before_discovery_does_not_cover_backfilled_message(self):
        self.check_reason("moderated_followup", candidate(has_moderation=True,
            inbound_at=NOW-timedelta(days=3), discovered_at=NOW-timedelta(seconds=10),
            evaluated_at=NOW-timedelta(days=2), evaluation_parse_succeeded=True,
            evaluation_verdict="success"))

    def test_unconfirmed_rpc_is_not_resend_permission(self):
        self.check_reason("send_outcome_unknown", candidate(batch_id=1,
            batch_inbound_message_id=301, batch_phase="sending"), known=False)

    def test_partial_delivery_does_not_hide_pending_rpc(self):
        self.check_reason("send_outcome_unknown", candidate(is_outgoing=True, batch_id=1,
            batch_inbound_message_id=301, batch_phase="sending"), known=False)

    def test_persisted_review_distinguishes_generation_and_delivery_errors(self):
        for error, reason in [("generation_failed", "generation_failed"),
                              ("delivery_unknown", "send_outcome_unknown")]:
            self.check_reason(reason, candidate(batch_id=1, batch_inbound_message_id=301,
                batch_phase="review", batch_error_kind=error, session_status="failed"))

    def test_generation_after_media_timeout_is_still_generation(self):
        self.check_reason("generation_pending", candidate(pending_media=True),
            live(generating=True, phase_started_at=NOW-timedelta(seconds=10)), review=False)

    def test_finish_callback_has_bounded_grace_even_after_session_stop(self):
        self.check_reason("completion_pending", candidate(batch_id=1,
            batch_inbound_message_id=301, batch_phase="finishing", finish_requested=True,
            batch_updated_at=NOW-timedelta(seconds=10), session_status="stopped"), review=False)

    def test_old_batch_does_not_describe_new_input(self):
        self.check_reason("processing_unconfirmed", candidate(batch_id=1,
            batch_inbound_message_id=300, batch_phase="sending"))

    def test_finished_batch_waits_for_evaluator_then_becomes_actionable(self):
        row = candidate(batch_id=1, batch_inbound_message_id=301, batch_phase="complete",
                        finish_requested=True, batch_updated_at=NOW-timedelta(seconds=20))
        self.check_reason("evaluation_pending", row, review=False)
        row["batch_updated_at"] = NOW-timedelta(hours=1)
        self.check_reason("evaluation_overdue", row)

    def test_lost_legacy_timer_outside_recovery_window_visible(self):
        self.check_reason("outside_recovery_window", candidate(
            inbound_at=NOW-timedelta(hours=51), discovered_at=NOW-timedelta(hours=51)))

    def test_custom_human_pause_is_honored(self):
        item = attention.classify_attention(candidate(), live(entering=True,
            phase_started_at=NOW-timedelta(minutes=20)), now=NOW, wait_grace_seconds=3600)
        self.assertEqual(item["reason"], "scheduled_wait_pending")
        self.assertFalse(item["needs_review"])

    def test_payload_does_not_leak_source_or_snapshot_fields(self):
        item = attention.classify_attention(candidate(text="secret", username="secret",
            last_error="secret", segments=["secret"]), live(password="secret"), now=NOW)
        self.assertNotIn("secret", str(item))


class InspectionTests(unittest.IsolatedAsyncioTestCase):
    def fake_pool(self, rows, settings=()):
        conn = MagicMock()
        conn.fetch = AsyncMock(side_effect=[settings, rows])
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock()
        transaction.__aexit__ = AsyncMock(return_value=False)
        conn.transaction.return_value = transaction
        lease = MagicMock()
        lease.__aenter__ = AsyncMock(return_value=conn)
        lease.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire.return_value = lease
        return pool, conn

    async def test_readonly_page_counts_and_cursor_are_explicit(self):
        pool, conn = self.fake_pool([candidate(is_outgoing=True), candidate(dialog_id=102),
                                     candidate(dialog_id=103)])
        with patch.object(attention.db, "get_pool", return_value=pool):
            result = await attention.inspect_attention(lambda _: live(), limit=2, now=NOW)
        conn.transaction.assert_called_once_with(readonly=True, isolation="repeatable_read")
        self.assertEqual(result["scanned_dialogs"], 2)
        self.assertEqual(result["next_after_dialog_id"], 102)
        self.assertTrue(result["has_more"])
        self.assertEqual(len(result["items"]), 1)
        conn.execute.assert_not_called()

    async def test_empty_candidate_page_is_not_global_all_clear(self):
        pool, _ = self.fake_pool([candidate(is_outgoing=True), candidate(dialog_id=102)])
        with patch.object(attention.db, "get_pool", return_value=pool):
            result = await attention.inspect_attention(limit=1, now=NOW)
        self.assertEqual(result["items"], [])
        self.assertTrue(result["has_more"])
        self.assertEqual(result["scope"], "dialog_page")

    async def test_snapshot_failure_is_unknown_not_healthy(self):
        pool, _ = self.fake_pool([candidate()])
        def broken(_):
            raise RuntimeError("private detail")
        with patch.object(attention.db, "get_pool", return_value=pool):
            result = await attention.inspect_attention(broken, now=NOW)
        self.assertEqual(result["snapshot_errors"], 1)
        self.assertEqual(result["items"][0]["reason"], "live_state_unknown")
        self.assertNotIn("private detail", str(result))

    async def test_timeout_is_not_swallowed_as_empty_success(self):
        pool, conn = self.fake_pool([])
        conn.fetch.side_effect = TimeoutError("deadline")
        with patch.object(attention.db, "get_pool", return_value=pool):
            with self.assertRaises(TimeoutError):
                await attention.inspect_attention(now=NOW)

    async def test_invalid_page_size_rejected_before_db(self):
        with patch.object(attention.db, "get_pool") as pool:
            for limit in [0, 501]:
                with self.assertRaises(ValueError):
                    await attention.inspect_attention(limit=limit)
            pool.assert_not_called()


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_only_and_page_parameters_validated(self):
        import httpx
        from fastapi import FastAPI
        from modules.autochat.routes import router
        app = FastAPI()
        app.include_router(router)
        snapshot = MagicMock(return_value=None)
        app.state.autochat_service = MagicMock(diagnostic_snapshot=snapshot)
        with patch.object(attention, "inspect_attention", AsyncMock(return_value={"read_only": True})) as inspect:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/autochat/attention?account_id=24&limit=2&after_dialog_id=100")
                self.assertEqual(response.status_code, 200)
                inspect.assert_awaited_once_with(snapshot, account_id=24, limit=2, after_dialog_id=100)
                for invalid in ["limit=501", "limit=0", "account_id=0", "after_dialog_id=-1"]:
                    self.assertEqual((await client.get("/autochat/attention?"+invalid)).status_code, 422)
                self.assertEqual((await client.post("/autochat/attention")).status_code, 405)


if __name__ == "__main__":
    unittest.main()
