"""Real manager lifecycle + delayed AutoRunner consumer, without external I/O."""
import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

from modules.worker_manager import service as manager_module
from modules.worker_auto_runner import service as runner_module


class ControlledWorker:
    def __init__(self, error=None):
        self.release = asyncio.Event()
        self.stop_seen = asyncio.Event()
        self.error = error

    async def run(self):
        await self.release.wait()
        if self.error:
            raise self.error

    async def stop(self):
        self.stop_seen.set()


class WorkerShutdownEventsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = manager_module.WorkerManager()
        self.manager._set_status = AsyncMock()
        self.manual_seen = asyncio.Event()
        self.intent = True

        async def set_intent(account_id, enabled):
            self.intent = enabled
            self.manual_seen.set()

        self.manager._set_desired_running = AsyncMock(side_effect=set_intent)
        self.events = []
        self.publish_patch = patch.object(manager_module.bus, "publish", AsyncMock(
            side_effect=lambda **event: self.events.append(event)))
        self.publish_patch.start()
        self.log_patch = patch.object(manager_module.log, "exception")
        self.log_patch.start()
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.publish_patch.stop()
        self.log_patch.stop()

    async def spawn(self, account_id=23, *, error=None, is_restart=False):
        worker = ControlledWorker(error)
        with patch.object(manager_module, "Worker", return_value=worker):
            await self.manager._spawn(
                account_id=account_id, account_name="test", session_data=None,
                proxy_primary="", proxy_fallback=None, api_id=1, api_hash="test",
                device_model="test", system_version="test", app_version="test",
                is_restart=is_restart,
            )
        slot = self.manager._slots[account_id]
        self.tasks.append(slot.task)
        return worker, slot

    def stopped_events(self):
        return [event for event in self.events
                if event["type"] == manager_module.EventType.WORKER_STOPPED]

    async def replay_to_auto_runner(self, schedule):
        # Simulate next-boot delivery of the durable events emitted at shutdown.
        runner = runner_module.WorkerAutoRunnerService(
            get_wrapper=lambda _: None, autochat_service=None)
        async def clear_schedule(*args, **kwargs):
            schedule.update(is_running=False, schedule=[])
        runner.stop = AsyncMock(side_effect=clear_schedule)
        with patch.object(runner_module, "_select_one", AsyncMock(return_value=schedule)):
            for event in self.events:
                await runner._dispatch_bus_event(event)
        return runner.stop

    async def test_manual_stop_still_emits_and_delayed_consumer_stops_schedule(self):
        worker, slot = await self.spawn()
        stopping = asyncio.create_task(self.manager.stop(23))
        await worker.stop_seen.wait()
        worker.release.set()
        await stopping
        self.assertFalse(self.intent)
        self.assertEqual(1, len(self.stopped_events()))
        schedule = {"is_running": True, "schedule": ["future-slot"]}
        stop = await self.replay_to_auto_runner(schedule)
        stop.assert_awaited_once_with(23, reason="worker_offline")
        self.assertEqual([], schedule["schedule"])

    async def test_app_shutdown_has_no_delayed_destructive_event(self):
        worker, slot = await self.spawn()
        schedule = {"is_running": True, "schedule": ["future-slot"], "session_id": "same-session"}
        before = copy.deepcopy(schedule)
        stopping = asyncio.create_task(self.manager.shutdown())
        await worker.stop_seen.wait()
        worker.release.set()
        await stopping
        self.assertTrue(self.intent)
        self.manager._set_desired_running.assert_not_called()
        self.manager._set_status.assert_awaited_with(23, manager_module.STATUS_STOPPED)
        self.assertEqual([], self.events)
        stop = await self.replay_to_auto_runner(schedule)
        stop.assert_not_called()
        self.assertEqual(before, schedule)
        self.assertNotIn(23, self.manager._slots)

    async def test_manual_then_shutdown_manual_wins(self):
        worker, slot = await self.spawn()
        manual = asyncio.create_task(self.manager.stop(23))
        await worker.stop_seen.wait()
        technical = asyncio.create_task(self.manager.shutdown())
        await asyncio.sleep(0)
        self.assertFalse(slot.suppress_stopped_event)
        worker.release.set()
        await asyncio.gather(manual, technical)
        self.assertFalse(self.intent)
        self.assertEqual(1, len(self.stopped_events()))

    async def test_shutdown_then_manual_manual_wins(self):
        worker, slot = await self.spawn()
        technical = asyncio.create_task(self.manager.shutdown())
        await worker.stop_seen.wait()
        self.assertTrue(slot.suppress_stopped_event)
        manual = asyncio.create_task(self.manager.stop(23))
        await self.manual_seen.wait()
        self.assertFalse(slot.suppress_stopped_event)
        worker.release.set()
        await asyncio.gather(manual, technical)
        self.assertFalse(self.intent)
        self.assertEqual(1, len(self.stopped_events()))

    async def test_retired_slot_cannot_emit_for_or_remove_replacement(self):
        old_worker, old_slot = await self.spawn()
        stopping = asyncio.create_task(self.manager.stop(23, preserve_intent=True))
        await old_worker.stop_seen.wait()
        new_worker, new_slot = await self.spawn()
        self.assertFalse(new_slot.suppress_stopped_event)
        self.manager._set_status.reset_mock()
        old_worker.release.set()
        await stopping
        self.assertIs(new_slot, self.manager._slots[23])
        self.manager._set_status.assert_not_called()
        self.assertEqual([], self.events)
        new_stop = asyncio.create_task(self.manager.stop(23))
        await new_worker.stop_seen.wait()
        new_worker.release.set()
        await new_stop
        self.assertEqual(1, len(self.stopped_events()))

    async def test_completion_waits_for_inflight_manual_intent_write(self):
        worker, slot = await self.spawn()
        technical = asyncio.create_task(self.manager.shutdown())
        await worker.stop_seen.wait()
        write_started, allow_write = asyncio.Event(), asyncio.Event()
        async def delayed_manual_write(account_id, enabled):
            write_started.set()
            await allow_write.wait()
            self.intent = enabled
        self.manager._set_desired_running.side_effect = delayed_manual_write
        manual = asyncio.create_task(self.manager.stop(23))
        await write_started.wait()
        worker.release.set()
        await asyncio.sleep(0)
        self.assertFalse(slot.task.done())
        self.assertEqual([], self.events)
        allow_write.set()
        await asyncio.gather(manual, technical)
        self.assertFalse(self.intent)
        self.assertEqual(1, len(self.stopped_events()))

    async def test_retired_manual_slot_cannot_emit_for_replacement(self):
        old_worker, old_slot = await self.spawn()
        stopping = asyncio.create_task(self.manager.stop(23))
        await old_worker.stop_seen.wait()
        new_worker, new_slot = await self.spawn()
        self.manager._set_status.reset_mock()
        old_worker.release.set()
        await stopping
        self.assertIs(new_slot, self.manager._slots[23])
        self.manager._set_status.assert_not_called()
        self.assertEqual([], self.events)

    async def test_final_crash_is_not_hidden_by_shutdown_flag(self):
        worker, slot = await self.spawn(error=RuntimeError("synthetic crash"), is_restart=True)
        stopping = asyncio.create_task(self.manager.shutdown())
        await worker.stop_seen.wait()
        worker.release.set()
        await stopping
        self.assertTrue(self.intent)
        self.assertEqual([manager_module.EventType.WORKER_CRASHED],
                         [event["type"] for event in self.events])
        self.manager._set_status.assert_awaited_with(23, manager_module.STATUS_CRASHED,
                                                   error="synthetic crash")

    async def test_session_expiry_still_clears_intent(self):
        worker, slot = await self.spawn(error=manager_module.SessionExpired("synthetic expiry"))
        stopping = asyncio.create_task(self.manager.shutdown())
        await worker.stop_seen.wait()
        worker.release.set()
        await stopping
        self.assertFalse(self.intent)
        self.manager._set_status.assert_awaited_with(23, manager_module.STATUS_SESSION_EXPIRED)
        self.assertEqual([], self.stopped_events())


if __name__ == "__main__":
    unittest.main()
