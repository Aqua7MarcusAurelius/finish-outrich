"""Offline demo callbacks: fake Redis/client, no DB, CRM, bus or network."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.errors import MessageNotModifiedError

from modules.report_bot.demo import handle_demo_callback

NONCE = "a" * 32
KEY = "demo_report:" + NONCE


class FakeRedis:
    def __init__(self):
        self.state = {"chat_id": -555, "message_id": 91, "caption_hidden": "ТЕСТ. Контакт скрыт",
                      "caption_revealed": "ТЕСТ. Контакт: вымышленный демонстрационный ник", "choice": None}
        self.payload = None
        self.seconds = 86400
        self.get_calls, self.set_calls = [], []
        self.expire_before_set = False

    async def get(self, key):
        self.get_calls.append(key)
        return self.payload if self.payload is not None else json.dumps(self.state).encode()

    async def ttl(self, key): return self.seconds

    async def set(self, key, value, **kwargs):
        self.set_calls.append((key, value, kwargs))
        if self.expire_before_set: return None
        self.state = json.loads(value)
        return True


def event(choice="take", **kwargs):
    values = {"data": f"demo:{NONCE}:{choice}".encode(), "chat_id": -555,
              "message_id": 91, "answer": AsyncMock()}
    values.update(kwargs)
    return SimpleNamespace(**values)


class DemoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.client = SimpleNamespace(edit_message=AsyncMock())
        self.patch = patch("modules.report_bot.demo.redis_mod.get_client", return_value=self.redis)
        self.patch.start(); self.addCleanup(self.patch.stop)

    async def test_take_reveals_and_saves_choice_before_edit(self):
        async def edit(chat_id, message_id, caption, **kwargs):
            self.assertEqual(self.redis.state["choice"], "take")
            self.assertEqual((chat_id, message_id, caption), (-555, 91, self.redis.state["caption_revealed"]))
            self.assertIsNone(kwargs["parse_mode"])
        self.client.edit_message.side_effect = edit
        query = event()
        await handle_demo_callback(query, self.client)
        self.assertEqual(self.redis.set_calls[0][2], {"xx": True, "keepttl": True})
        self.assertEqual(self.redis.seconds, 86400)
        self.assertIn("CRM не изменяется", query.answer.call_args.args[0])

    async def test_no_take_and_invalid_hide_contact_again(self):
        for choice in ("take", "no_take", "invalid"):
            await handle_demo_callback(event(choice), self.client)
            caption = self.client.edit_message.call_args.args[2]
            self.assertEqual(caption, self.redis.state["caption_revealed" if choice == "take" else "caption_hidden"])
            buttons = self.client.edit_message.call_args.kwargs["buttons"][0]
            self.assertEqual(sum(b.text.startswith("✅ ") for b in buttons), 1)
            self.assertEqual([b.data for b in buttons], [f"demo:{NONCE}:{c}".encode() for c in ("take", "no_take", "invalid")])
            chosen = next(b for b in buttons if b.text.startswith("✅ "))
            self.assertTrue(chosen.data.endswith(choice.encode()))

    async def test_repeat_selection_message_not_modified_is_success(self):
        self.client.edit_message.side_effect = MessageNotModifiedError(request=None)
        query = event()
        await handle_demo_callback(query, self.client)
        self.assertEqual(self.redis.state["choice"], "take")
        self.assertFalse(query.answer.call_args.kwargs["alert"])

    async def test_wrong_chat_or_anchor_never_changes_state_or_edits(self):
        for kwargs in ({"chat_id": -777}, {"message_id": 92}, {"chat_id": None}):
            query = event(**kwargs)
            await handle_demo_callback(query, self.client)
            self.assertTrue(query.answer.call_args.kwargs["alert"])
        self.assertEqual(self.redis.set_calls, [])
        self.client.edit_message.assert_not_awaited()

    async def test_missing_or_expired_and_nonexpiring_state_fail_closed(self):
        for seconds in (-2, -1, 0):
            self.redis.seconds = seconds
            query = event()
            await handle_demo_callback(query, self.client)
            self.assertEqual(query.answer.call_args.args[0], "Тест завершён")
        self.redis.payload = b""
        self.redis.seconds = 100
        await handle_demo_callback(event(), self.client)
        self.assertEqual(self.redis.set_calls, [])
        self.client.edit_message.assert_not_awaited()

    async def test_expiry_between_read_and_store_does_not_resurrect_or_edit(self):
        self.redis.expire_before_set = True
        query = event()
        await handle_demo_callback(query, self.client)
        self.assertEqual(query.answer.call_args.args[0], "Тест завершён")
        self.assertIsNone(self.redis.state["choice"])
        self.client.edit_message.assert_not_awaited()

    async def test_malformed_callback_never_reads_redis(self):
        for raw in (b"m:take:1", b"demo:1:take", f"demo:{NONCE}:delete".encode(),
                    f"demo:{NONCE}:take:more".encode(), b"demo:\xff:take", b"", None):
            query = event(data=raw)
            await handle_demo_callback(query, self.client)
            self.assertTrue(query.answer.call_args.kwargs["alert"])
        self.assertEqual(self.redis.get_calls, [])
        self.client.edit_message.assert_not_awaited()

    async def test_corrupt_or_unbound_state_never_edits(self):
        for payload in (b"{", b"null", b"[]", b"{}", b'{"chat_id":"-555"}',
                        json.dumps({**self.redis.state, "caption_revealed": "x" * 1025}).encode()):
            self.redis.payload = payload
            query = event()
            await handle_demo_callback(query, self.client)
            self.assertEqual(query.answer.call_args.args[0], "Тест завершён")
        self.assertEqual(self.redis.set_calls, [])
        self.client.edit_message.assert_not_awaited()

    async def test_none_client_does_not_read_or_change_state(self):
        await handle_demo_callback(event(), None)
        self.assertEqual(self.redis.get_calls, [])
        self.assertEqual(self.redis.set_calls, [])

    async def test_edit_failure_and_timeout_report_failure_without_retry(self):
        for error in (RuntimeError("network unavailable"), asyncio.TimeoutError()):
            self.client.edit_message.reset_mock()
            self.client.edit_message.side_effect = error
            query = event()
            await handle_demo_callback(query, self.client)
            self.assertEqual(self.redis.state["choice"], "take")
            self.assertEqual(self.client.edit_message.await_count, 1)
            self.assertIn("карточка не обновилась", query.answer.call_args.args[0])
            self.assertTrue(query.answer.call_args.kwargs["alert"])

    async def test_cancellation_propagates_and_never_retries_edit(self):
        self.client.edit_message.side_effect = asyncio.CancelledError()
        query = event()
        with self.assertRaises(asyncio.CancelledError):
            await handle_demo_callback(query, self.client)
        self.assertEqual(self.client.edit_message.await_count, 1)
        query.answer.assert_not_awaited()

    async def test_concurrent_take_then_invalid_cannot_leave_stale_reveal(self):
        entered, release = asyncio.Event(), asyncio.Event()
        shown = []
        async def edit(chat_id, message_id, caption, **kwargs):
            if not shown:
                entered.set()
                await release.wait()
            shown.append(caption)
        self.client.edit_message.side_effect = edit
        first = asyncio.create_task(handle_demo_callback(event("take"), self.client))
        await entered.wait()
        second = asyncio.create_task(handle_demo_callback(event("invalid"), self.client))
        await asyncio.sleep(0)
        self.assertEqual(self.redis.state["choice"], "take")
        self.assertEqual(len(self.redis.get_calls), 1)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.redis.state["choice"], "invalid")
        self.assertEqual(shown, [self.redis.state["caption_revealed"], self.redis.state["caption_hidden"]])

    async def test_no_database_bus_or_crm_access(self):
        with patch("core.db.get_pool", side_effect=AssertionError("DB forbidden")) as db, \
             patch("core.bus.publish", new_callable=AsyncMock) as bus, \
             patch("modules.partner_crm.service.PartnerCrmService.push_lead_moderated", new_callable=AsyncMock) as crm:
            await handle_demo_callback(event(), self.client)
        db.assert_not_called(); bus.assert_not_awaited(); crm.assert_not_awaited()
        self.assertEqual(self.redis.state["choice"], "take")

    async def test_redis_outage_answers_safely_without_edit(self):
        with patch("modules.report_bot.demo.redis_mod.get_client", side_effect=RuntimeError("offline")):
            query = event()
            await handle_demo_callback(query, self.client)
        self.assertEqual(query.answer.call_args.args[0], "Тест временно недоступен")
        self.client.edit_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
