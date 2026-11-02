"""Transport-boundary regression tests. No real DB, LLM, Telegram or Redis."""
import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from modules.autochat import session as module


class ReplyRecoveryTests(IsolatedAsyncioTestCase):
    def make(self, wrapper=None):
        s = module.AutoChatSession(row={"id": 1, "account_id": 2, "dialog_id": 3,
            "telegram_user_id": 4, "target_username": "test", "system_prompt": "test"},
            get_wrapper=lambda _: wrapper, on_finished_by_llm=AsyncMock())
        s._persist_state = AsyncMock()
        s._sleep_keeping_typing = AsyncMock()
        s._fail = AsyncMock()
        return s

    def pool(self, results):
        conn = MagicMock()
        conn.fetchval = AsyncMock(side_effect=results)
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
        return pool

    async def test_ready_reply_disconnected_is_deferred_not_uncertain(self):
        s = self.make()
        await s._send_queue.put({"batch_id": 9, "index": 1, "total": 1, "text": "prepared"})
        await s._send_queue.put(None)
        with patch.object(module.reply_journal, "pending", AsyncMock(return_value={"id": 9, "phase": "ready"})), \
             patch.object(module.reply_journal, "review", AsyncMock()) as review:
            await s._sender_loop()
        review.assert_not_awaited()
        s._fail.assert_not_awaited()
        self.assertEqual(s._last_error_kind, "send_deferred")

    async def test_rpc_timeout_is_uncertain_never_lead_invalid(self):
        wrapper = SimpleNamespace(set_typing=AsyncMock(), cancel_typing=AsyncMock(),
                                  send_message=AsyncMock(side_effect=TimeoutError()))
        s = self.make(wrapper)
        await s._send_queue.put({"batch_id": 9, "index": 1, "total": 1, "text": "prepared"})
        await s._send_queue.put(None)
        with patch.object(module, "_get_setting_int", AsyncMock(return_value=0)), \
             patch.object(module.reply_journal, "before_send", AsyncMock(return_value=True)), \
             patch.object(module.reply_journal, "pending", AsyncMock(return_value={"id": 9, "phase": "sending"})), \
             patch.object(module.reply_journal, "review", AsyncMock()) as review:
            await s._sender_loop()
        wrapper.send_message.assert_awaited_once()
        review.assert_awaited_once_with(9, 3, "delivery_unknown")
        s._fail.assert_not_awaited()

    async def test_stale_segment_never_calls_transport(self):
        wrapper = SimpleNamespace(set_typing=AsyncMock(), cancel_typing=AsyncMock(), send_message=AsyncMock())
        s = self.make(wrapper)
        with patch.object(module, "_get_setting_int", AsyncMock(return_value=0)), \
             patch.object(module.reply_journal, "before_send", AsyncMock(return_value=False)):
            await s._send_segment({"batch_id": 9, "index": 1, "total": 1, "text": "old"})
        wrapper.send_message.assert_not_awaited()

    async def test_ready_partial_reply_resumes_original_remaining_segment(self):
        wrapper = SimpleNamespace(get_history=AsyncMock(return_value=[SimpleNamespace(id=10), SimpleNamespace(id=11)]))
        s = self.make(wrapper)
        batch = {"id": 9, "phase": "ready", "inbound_message_id": 8, "next_index": 1,
                 "segments": [{"text": "sent", "telegram_message_id": 11}, {"text": "remaining"}],
                 "finish_requested": False, "parent_id": "original"}
        with patch.object(module.reply_journal, "enabled", AsyncMock(return_value=True)), \
             patch.object(module.reply_journal, "pending", AsyncMock(return_value=batch)), \
             patch.object(module.db, "get_pool", return_value=self.pool([2, 10])), \
             patch.object(module, "_call_llm_with_retries", AsyncMock()) as llm:
            self.assertEqual(await s.recover_reply(), "resumed")
        self.assertEqual(s._send_queue.qsize(), 1)
        self.assertEqual((await s._send_queue.get())["text"], "remaining")
        llm.assert_not_awaited()

    async def test_unknown_rpc_cannot_be_replayed_on_restart(self):
        wrapper = SimpleNamespace(get_history=AsyncMock(), send_message=AsyncMock())
        s = self.make(wrapper)
        with patch.object(module.reply_journal, "enabled", AsyncMock(return_value=True)), \
             patch.object(module.reply_journal, "pending", AsyncMock(return_value={"id": 9, "phase": "sending"})), \
             patch.object(module.reply_journal, "review", AsyncMock()) as review:
            self.assertEqual(await s.recover_reply(), "review")
        wrapper.send_message.assert_not_awaited()
        wrapper.get_history.assert_not_awaited()
        review.assert_awaited_once()
        self.assertTrue(s._send_queue.empty())

    async def test_busy_sender_is_not_an_empty_queue(self):
        s = self.make()
        s._sending = True
        self.assertTrue(s._send_queue.empty())
        self.assertTrue(s.diagnostic_snapshot()["sending"])
        self.assertEqual(await s.recover_reply(), "busy")

    async def test_missing_remote_history_cannot_resume(self):
        wrapper = SimpleNamespace(get_history=AsyncMock(return_value=[SimpleNamespace(id=10), SimpleNamespace(id=11)]))
        s = self.make(wrapper)
        with patch.object(module.reply_journal, "enabled", AsyncMock(return_value=True)), \
             patch.object(module.reply_journal, "pending", AsyncMock(return_value={"id": 9, "phase": "ready", "inbound_message_id": 8})), \
             patch.object(module.db, "get_pool", return_value=self.pool([1, 10])):
            self.assertEqual(await s.recover_reply(), "history_not_caught_up")
        self.assertTrue(s._send_queue.empty())

    async def test_account_retry_waits_for_history_and_enqueues_once(self):
        from modules.autochat import service as service_module
        wrapper = SimpleNamespace(get_history=AsyncMock(return_value=[SimpleNamespace(id=10), SimpleNamespace(id=11)]))
        s = self.make(wrapper)
        svc = service_module.AutoChatService(get_wrapper=lambda _: wrapper)
        svc._sessions_by_id[s.id] = s
        batch = {"id": 9, "phase": "ready", "inbound_message_id": 8, "next_index": 1,
                 "segments": [{"text": "sent", "telegram_message_id": 11}, {"text": "remaining"}],
                 "finish_requested": False, "parent_id": "original"}
        pool = self.pool([1, 10, 1, 10, 2, 10])
        pool.acquire.return_value.__aenter__.return_value.fetch = AsyncMock(return_value=[{"session_id": s.id}])
        with patch.object(module.reply_journal, "enabled", AsyncMock(return_value=True)), \
             patch.object(module.reply_journal, "pending", AsyncMock(return_value=batch)), \
             patch.object(module.db, "get_pool", return_value=pool), \
             patch.object(service_module.asyncio, "sleep", AsyncMock()) as sleep:
            await svc._on_worker_started(2, {})
        self.assertEqual(wrapper.get_history.await_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 5])
        self.assertEqual(s._send_queue.qsize(), 1)
        self.assertEqual((await s._send_queue.get())["text"], "remaining")

    async def test_history_retry_budget_is_finite(self):
        from modules.autochat import service as service_module
        svc = service_module.AutoChatService(get_wrapper=lambda _: None)
        session = SimpleNamespace(id=1, recover_reply=AsyncMock(return_value="history_not_caught_up"))
        svc._sessions_by_id[1] = session
        pool = self.pool([])
        pool.acquire.return_value.__aenter__.return_value.fetch = AsyncMock(return_value=[{"session_id": 1}])
        with patch.object(module.reply_journal, "enabled", AsyncMock(return_value=True)), \
             patch.object(module.db, "get_pool", return_value=pool), \
             patch.object(service_module.asyncio, "sleep", AsyncMock()) as sleep:
            await svc._on_worker_started(2, {})
        self.assertEqual(session.recover_reply.await_count, 4)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 5, 15])

    async def test_anchor_outside_history_window_is_not_retry_loop(self):
        from modules.autochat import service as service_module
        wrapper = SimpleNamespace(get_history=AsyncMock(return_value=[SimpleNamespace(id=11)]))
        s = self.make(wrapper)
        svc = service_module.AutoChatService(get_wrapper=lambda _: wrapper)
        svc._sessions_by_id[s.id] = s
        pool = self.pool([1, 10])
        pool.acquire.return_value.__aenter__.return_value.fetch = AsyncMock(return_value=[{"session_id": s.id}])
        with patch.object(module.reply_journal, "enabled", AsyncMock(return_value=True)), \
             patch.object(module.reply_journal, "pending", AsyncMock(return_value={"id": 9, "phase": "ready", "inbound_message_id": 8})), \
             patch.object(module.db, "get_pool", return_value=pool), \
             patch.object(service_module.asyncio, "sleep", AsyncMock()) as sleep:
            await svc._on_worker_started(2, {})
        wrapper.get_history.assert_awaited_once()
        sleep.assert_not_awaited()
        self.assertTrue(s._send_queue.empty())

    async def test_legacy_session_is_not_replayed_by_new_recovery(self):
        s = self.make()
        with patch.object(module.reply_journal, "enabled", AsyncMock(return_value=True)), \
             patch.object(module.reply_journal, "pending", AsyncMock(return_value=None)):
            self.assertEqual(await s.recover_reply(), "no_journal")

    async def test_cancelled_recovery_media_wait_preserves_intent_for_next_process(self):
        wrapper = SimpleNamespace(get_history=AsyncMock(return_value=[SimpleNamespace(id=10)]))
        batch = {"id": 9, "phase": "generating", "inbound_message_id": 8}
        async def pending(_):
            return batch if batch["phase"] == "generating" else None
        async def abandon(_):
            batch["phase"] = "superseded"
            return True
        first = self.make(wrapper)
        first._wait_for_transcriptions = AsyncMock(side_effect=asyncio.CancelledError())
        first._generate_and_enqueue = AsyncMock()
        second = self.make(wrapper)
        second._wait_for_transcriptions = AsyncMock()
        second._generate_and_enqueue = AsyncMock(return_value=True)
        with patch.object(module.reply_journal, "enabled", AsyncMock(return_value=True)), \
             patch.object(module.reply_journal, "pending", AsyncMock(side_effect=pending)), \
             patch.object(module.reply_journal, "abandon_generation", AsyncMock(side_effect=abandon)), \
             patch.object(module.db, "get_pool", return_value=self.pool([1, 10, 1, 10])):
            with self.assertRaises(asyncio.CancelledError):
                await first.recover_reply()
            first._generate_and_enqueue.assert_not_awaited()
            self.assertEqual(batch["phase"], "generating")
            self.assertEqual(await second.recover_reply(), "regenerated_intent")
        second._generate_and_enqueue.assert_awaited_once()

    async def test_new_input_during_long_llm_reenters_after_idle_expired(self):
        s = self.make()
        s.in_chat = True
        s._reset_idle = MagicMock()
        old = {"id": 9, "phase": "generating"}
        newer = {"id": 10, "phase": "generating"}
        async def llm(*args, **kwargs):
            # The new inbound has its own durable intent. During this slow LLM
            # call the ordinary idle timer has legitimately left the chat.
            s.in_chat = False
            return "<msg>stale reply</msg>"
        prompts = SimpleNamespace(has_reply=lambda: True, reply_template="test")
        with patch.object(module, "load_for_account", AsyncMock(return_value=prompts)), \
             patch.object(module.reply_journal, "enabled", AsyncMock(return_value=True)), \
             patch.object(module.reply_journal, "begin", AsyncMock(return_value=old)), \
             patch.object(module.reply_journal, "store_result", AsyncMock(return_value=None)), \
             patch.object(module.reply_journal, "pending", AsyncMock(return_value=newer)), \
             patch.object(module.db, "get_pool", return_value=self.pool([])), \
             patch.object(module, "build_conversation_context", AsyncMock(return_value=[])), \
             patch.object(module.bus, "publish", AsyncMock(return_value={"id": "request"})), \
             patch.object(module, "_call_llm_with_retries", AsyncMock(side_effect=llm)), \
             patch.object(module, "_get_setting_int", AsyncMock(return_value=0)):
            self.assertFalse(await s._generate_and_enqueue())
            self.assertIsNotNone(s._enter_task)
            await s._enter_task
        self.assertTrue(s.in_chat)
        self.assertTrue(s._planner_bump.is_set())
        self.assertTrue(s._send_queue.empty())

    async def test_stale_generation_does_not_rearm_without_new_pending_intent(self):
        s = self.make()
        with patch.object(module.reply_journal, "pending", AsyncMock(return_value=None)):
            await s._rearm_pending_input()
        self.assertIsNone(s._enter_task)
        self.assertFalse(s._planner_bump.is_set())

    async def test_finish_waits_for_history_and_does_not_stop(self):
        s = self.make()
        s._publish_finished_by_llm = AsyncMock()
        with patch.object(module.reply_journal, "start_finish", AsyncMock(return_value=False)):
            await s._finish_reply("parent", 9)
        s._publish_finished_by_llm.assert_not_awaited()
        s._on_finished_by_llm.assert_not_awaited()

    async def test_failed_finish_publication_requires_review(self):
        s = self.make()
        s._publish_finished_by_llm = AsyncMock(return_value=False)
        with patch.object(module.reply_journal, "start_finish", AsyncMock(return_value=True)), \
             patch.object(module.reply_journal, "review", AsyncMock()) as review:
            await s._finish_reply("parent", 9)
        review.assert_awaited_once_with(9, 3, "finish_delivery_unknown")
        s._on_finished_by_llm.assert_not_awaited()


if __name__ == "__main__":
    import unittest
    unittest.main()
