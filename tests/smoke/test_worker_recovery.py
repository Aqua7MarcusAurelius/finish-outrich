"""Worker recovery uses explicit intent and actual tasks, without external I/O."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from modules.worker_manager import service as module


class WorkerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = module.WorkerManager()
        self.row = dict(
            id=24, name="TEST_01", is_active=True, worker_desired_running=False,
            session_data=None, proxy_primary="", proxy_fallback=None,
            api_id=1, api_hash="test", device_model="test", system_version="test",
            app_version="test", lang_code="en", system_lang_code="en", lang_pack="",
        )
        self.conn = MagicMock()
        self.conn.fetchrow = AsyncMock(side_effect=lambda *args: dict(self.row))
        self.conn.fetch = AsyncMock(side_effect=lambda *args: [dict(self.row)])

        async def execute(sql, account_id, enabled):
            self.row["worker_desired_running"] = enabled
            return "UPDATE 1"

        self.conn.execute = AsyncMock(side_effect=execute)
        self.conn.__aenter__ = AsyncMock(return_value=self.conn)
        self.conn.__aexit__ = AsyncMock(return_value=None)
        self.pool = MagicMock()
        self.pool.acquire.return_value = self.conn
        self.patch = patch.object(module.db, "get_pool", return_value=self.pool)
        self.patch.start()
        self.manager._set_status = AsyncMock()
        self.manager._get_status = AsyncMock(return_value="stopped")
        self.manager._spawn = AsyncMock()

    def tearDown(self):
        self.patch.stop()

    async def test_explicit_start_enables_but_recover_cannot_enable(self):
        with self.assertRaises(module.RecoveryDisabled):
            await self.manager.start(24, recover=True)
        self.manager._spawn.assert_not_called()
        self.assertFalse(self.row["worker_desired_running"])
        await self.manager.start(24)
        self.assertTrue(self.row["worker_desired_running"])
        self.manager._spawn.assert_awaited_once()

    async def test_offline_manual_stop_wins_over_stale_recovery_snapshot(self):
        self.row["worker_desired_running"] = True
        self.assertEqual([24], [w["account_id"] for w in
                               (await self.manager.readiness())["workers"] if w["recoverable"]])
        await self.manager.stop(24)
        with self.assertRaises(module.RecoveryDisabled):
            await self.manager.start(24, recover=True)
        self.manager._spawn.assert_not_called()

    async def test_expired_or_service_cannot_recover(self):
        self.row["worker_desired_running"] = True
        self.manager._get_status.return_value = "session_expired"
        with self.assertRaises(module.RecoveryDisabled):
            await self.manager.start(24, recover=True)
        self.row["name"] = module.settings.SERVICE_ACCOUNT_NAME
        with self.assertRaises(module.ServiceAccountNotStartable):
            await self.manager.start(24, recover=True)
        self.manager._spawn.assert_not_called()

    async def test_shutdown_preserves_intent(self):
        self.row["worker_desired_running"] = True
        task = asyncio.create_task(asyncio.sleep(0))
        worker = SimpleNamespace(stop=AsyncMock(), wrapper=MagicMock())
        self.manager._slots[24] = module._Slot(worker, task, 0)
        await self.manager.shutdown()
        self.assertTrue(self.row["worker_desired_running"])
        self.conn.execute.assert_not_called()

    async def test_cached_running_is_not_actual_readiness(self):
        self.row["worker_desired_running"] = True
        self.manager._get_status.return_value = "running"
        state = await self.manager.readiness()
        self.assertFalse(state["ready"])
        self.assertEqual(0, state["connected"])
        task = asyncio.create_task(asyncio.sleep(10))
        wrapper = MagicMock()
        wrapper.is_connected.return_value = True
        self.manager._slots[24] = module._Slot(SimpleNamespace(wrapper=wrapper), task, 0)
        self.assertTrue((await self.manager.readiness())["ready"])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse((await self.manager.readiness())["ready"])

    async def test_spawn_failure_restores_previous_intent(self):
        self.manager._spawn.side_effect = RuntimeError("spawn failed")
        with self.assertRaises(RuntimeError):
            await self.manager.start(24)
        self.assertFalse(self.row["worker_desired_running"])

    async def test_session_expiry_clears_durable_intent(self):
        self.row["worker_desired_running"] = True
        worker = MagicMock()
        worker.run = AsyncMock(side_effect=module.SessionExpired("expired"))
        with patch.object(module, "Worker", return_value=worker):
            await module.WorkerManager._spawn(
                self.manager, account_id=24, account_name="test", session_data=None,
                proxy_primary="", proxy_fallback=None, api_id=1, api_hash="test",
                device_model="test", system_version="test", app_version="test",
            )
            await self.manager._slots[24].task
        self.assertFalse(self.row["worker_desired_running"])
        self.manager._set_status.assert_awaited_with(24, "session_expired")


if __name__ == "__main__":
    unittest.main()
