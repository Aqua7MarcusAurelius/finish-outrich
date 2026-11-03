"""Offline single-use demo authorization and dispatch isolation."""
import asyncio
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from api.routes.system import send_report_demo
from modules.report_bot.service import ReportBotService


class DemoRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.token = 'a' * 64
        self.job = {'run_id': 'b' * 32, 'families': ['SAD']}
        self.redis = SimpleNamespace(getdel=AsyncMock(return_value=json.dumps(self.job)), set=AsyncMock())
        self.service = SimpleNamespace(_client=SimpleNamespace(is_connected=lambda: True), _demo_tasks=set())
        self.request = SimpleNamespace(headers={'authorization': 'Bearer ' + self.token},
            app=SimpleNamespace(state=SimpleNamespace(report_bot_service=self.service)))
        self.patch = patch('api.routes.system.redis_mod.get_client', return_value=self.redis)
        self.patch.start();self.addCleanup(self.patch.stop)

    async def test_no_grant_fails_closed_even_without_configured_api_token(self):
        self.request.headers = {}
        with self.assertRaises(HTTPException) as got:
            await send_report_demo(self.request)
        self.assertEqual(got.exception.status_code, 401)
        self.redis.getdel.assert_not_awaited()

    async def test_disconnected_bot_does_not_spend_grant(self):
        self.service._client = None
        with self.assertRaises(HTTPException) as got:
            await send_report_demo(self.request)
        self.assertEqual(got.exception.status_code, 503)
        self.redis.getdel.assert_not_awaited()

    async def test_reused_or_expired_grant_cannot_dispatch(self):
        self.redis.getdel.return_value = None
        with self.assertRaises(HTTPException) as got:
            await send_report_demo(self.request)
        self.assertEqual(got.exception.status_code, 401)

    async def test_valid_grant_uses_hashed_key_and_existing_client(self):
        with patch('modules.report_bot.demo_sender.send_demo_batch', new=AsyncMock()) as sender:
            result = await send_report_demo(self.request)
            await asyncio.gather(*list(self.service._demo_tasks))
        self.assertTrue(result['accepted'])
        self.redis.getdel.assert_awaited_once_with('report_demo_grant:' + hashlib.sha256(self.token.encode()).hexdigest())
        sender.assert_awaited_once_with(self.service, self.job)

    async def test_untrusted_grant_shape_cannot_dispatch(self):
        self.redis.getdel.return_value = json.dumps({'run_id': '../bad', 'families': ['SAD']})
        with self.assertRaises(HTTPException) as got:
            await send_report_demo(self.request)
        self.assertEqual(got.exception.status_code, 422)

    async def test_sender_failure_does_not_overwrite_partial_send_ledger(self):
        with patch('modules.report_bot.demo_sender.send_demo_batch', new=AsyncMock(side_effect=RuntimeError('failure'))):
            await send_report_demo(self.request)
            await asyncio.gather(*list(self.service._demo_tasks))
        self.assertTrue(self.redis.set.await_args.kwargs['nx'])

    async def test_demo_callback_never_reaches_production_db_or_crm(self):
        bot = ReportBotService(MagicMock())
        event = SimpleNamespace(data=b'demo:' + b'a' * 32 + b':take')
        with patch('modules.report_bot.demo.handle_demo_callback', new=AsyncMock()) as handler, \
             patch('modules.report_bot.service.db.get_pool', side_effect=AssertionError('production DB')):
            await bot._on_callback(event)
        handler.assert_awaited_once_with(event, None)


if __name__ == '__main__': unittest.main()
