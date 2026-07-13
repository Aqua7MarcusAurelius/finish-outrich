"""Smoke-тесты C1 + C2 (2026-07-13) для worker_auto_runner.

C1 — пропись не должна самостопиться после первого лида на повторном
     старте: вердикт `_self_stop_requested` чистится на всех входах/выходах
     (`start`, `stop`, `_do_self_stop`).
C2 — `stop()`/`shutdown()` не должны падать с `asyncio.CancelledError`
     (в Python 3.12+ он НЕ наследник Exception) при отмене loop-task'и;
     единый helper `_cancel_and_wait` глушит его.

БД не трогаем: модульные DB-функции монкипатчим на async-заглушки.

Запуск:
    docker compose exec -T app python -m tests.smoke.test_auto_runner_c1_c2
"""
from __future__ import annotations

import asyncio
import sys

import modules.worker_auto_runner.service as ar


def _patch_db(monkey: dict) -> None:
    """Подменить DB-функции модуля + bus.publish на безобидные заглушки.
    `monkey` — dict для сохранения оригиналов (восстановить в finally)."""
    async def _ensure_row(account_id):  # noqa: ANN001
        return None

    async def _select_one(account_id):  # noqa: ANN001
        return {
            "account_id": account_id,
            "is_running": True,
            "queue": [],
            "window_hours": 12,
            "min_leads": 15,
            "max_leads": 18,
            "warmup_minutes": 10,
            "active_start_hour_msk": 8,
            "active_end_hour_msk": 23,
            "session_started_at": None,
            "session_ends_at": None,
            "schedule": [],
            "cooldown_until": None,
            "cooldown_reason": None,
            "last_action_at": None,
            "last_status": None,
            "updated_at": None,
        }

    async def _account_exists(account_id):  # noqa: ANN001
        return True

    async def _set_running(account_id, is_running, last_status=None):  # noqa: ANN001
        return None

    async def _clear_session(account_id):  # noqa: ANN001
        return None

    async def _write_session(account_id, *, started_at, ends_at, schedule):  # noqa: ANN001
        return None

    async def _publish(**kwargs):  # noqa: ANN001
        return {"id": "evt"}

    for name, fn in (
        ("_ensure_row", _ensure_row),
        ("_select_one", _select_one),
        ("_account_exists", _account_exists),
        ("_set_running", _set_running),
        ("_clear_session", _clear_session),
        ("_write_session", _write_session),
    ):
        monkey[name] = getattr(ar, name)
        setattr(ar, name, fn)
    monkey["bus.publish"] = ar.bus.publish
    ar.bus.publish = _publish  # type: ignore


def _unpatch_db(monkey: dict) -> None:
    for name, orig in monkey.items():
        if name == "bus.publish":
            ar.bus.publish = orig  # type: ignore
        else:
            setattr(ar, name, orig)


async def test_cancel_and_wait_swallows_cancelled() -> bool:
    """`_cancel_and_wait` не даёт CancelledError утечь наверх."""
    async def _inf():
        await asyncio.Event().wait()

    task = asyncio.create_task(_inf())
    await asyncio.sleep(0)  # дать задаче стартовать
    try:
        await ar._cancel_and_wait(task)
    except BaseException as e:  # noqa: BLE001
        print(f"  FAIL: _cancel_and_wait raised {type(e).__name__}")
        return False
    return task.done()


async def test_stop_no_raise_and_clears_flag() -> bool:
    """stop() отменяет живой loop-task БЕЗ CancelledError наверх и чистит
    устаревший self-stop вердикт (C1 + C2 вместе)."""
    monkey: dict = {}
    _patch_db(monkey)
    try:
        svc = ar.WorkerAutoRunnerService(
            get_wrapper=lambda a: object(), autochat_service=None,
        )
        acc = 999001

        async def _loop():
            await asyncio.Event().wait()

        svc._tasks[acc] = asyncio.create_task(_loop())
        svc._self_stop_requested[acc] = "stale_verdict"
        await asyncio.sleep(0)

        try:
            await svc.stop(acc, reason="test")
        except BaseException as e:  # noqa: BLE001
            print(f"  FAIL: stop() raised {type(e).__name__}")
            return False

        if acc in svc._self_stop_requested:
            print("  FAIL: stale self-stop flag not cleared by stop()")
            return False
        if acc in svc._tasks:
            print("  FAIL: task not removed by stop()")
            return False
        return True
    finally:
        _unpatch_db(monkey)


async def test_do_self_stop_clears_flag() -> bool:
    """_do_self_stop вычищает вердикт (C1) — иначе он переживёт границу
    сессии и самостопнет следующий запуск после первого лида."""
    monkey: dict = {}
    _patch_db(monkey)
    try:
        svc = ar.WorkerAutoRunnerService(
            get_wrapper=lambda a: None, autochat_service=None,
        )
        acc = 999002
        svc._self_stop_requested[acc] = "session_completed"
        await svc._do_self_stop(acc, "session_completed")
        if acc in svc._self_stop_requested:
            print("  FAIL: _do_self_stop did not clear flag")
            return False
        return True
    finally:
        _unpatch_db(monkey)


async def test_start_clears_stale_flag() -> bool:
    """start() стирает устаревший вердикт до создания loop-task — свежий
    старт не должен унаследовать session_completed/needs_operator от прошлой
    сессии (иначе самостоп после первого лида — исходный баг C1)."""
    monkey: dict = {}
    _patch_db(monkey)
    try:
        svc = ar.WorkerAutoRunnerService(
            get_wrapper=lambda a: object(), autochat_service=None,
        )
        acc = 999003
        svc._self_stop_requested[acc] = "session_completed"

        # Подменяем сам loop на no-op, чтобы не крутить реальную итерацию.
        async def _noop_loop(account_id):  # noqa: ANN001
            return None

        svc._account_loop = _noop_loop  # type: ignore

        await svc.start(acc)

        ok = acc not in svc._self_stop_requested
        if not ok:
            print("  FAIL: start() did not clear stale self-stop flag")

        # cleanup: гасим созданную task
        task = svc._tasks.pop(acc, None)
        if task is not None and not task.done():
            await ar._cancel_and_wait(task)
        return ok
    finally:
        _unpatch_db(monkey)


async def _run_all() -> int:
    results = [
        ("C2: _cancel_and_wait swallows CancelledError",
         await test_cancel_and_wait_swallows_cancelled()),
        ("C2+C1: stop() no raise + clears flag",
         await test_stop_no_raise_and_clears_flag()),
        ("C1: _do_self_stop clears flag",
         await test_do_self_stop_clears_flag()),
        ("C1: start() clears stale flag",
         await test_start_clears_stale_flag()),
    ]
    print()
    for name, ok in results:
        print(f"  {name:50} → {'PASS' if ok else 'FAIL'}")
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print()
    if passed == total:
        print(f"ALL {total} C1/C2 TESTS PASSED")
        return 0
    print(f"FAILED: {total - passed}/{total}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all()))
