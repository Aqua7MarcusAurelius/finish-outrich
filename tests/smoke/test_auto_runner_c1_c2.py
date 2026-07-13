"""Smoke-тесты C1 + C2 для worker_auto_runner (обновлено 2026-07-13).

C1 — самостоп по needs_operator. Раньше reason писался в долгоживущий словарь
     `_self_stop_requested` и требовал ручной чистки на всех входах/выходах
     (класс багов «вердикт пережил границу сессии»). Теперь `_try_start_dialog`
     ВОЗВРАЩАЕТ reason значением, а `_account_loop` сам решает стопаться — side-
     channel убран, класс багов исчез по построению. Тест проверяет контракт
     возврата: needs_operator → reason, benign/success → None.
C2 — `stop()`/`shutdown()` не падают с `asyncio.CancelledError` (в Python 3.12+
     он НЕ наследник Exception) при отмене loop-task'и; helper `_cancel_and_wait`
     его глушит.

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
            "account_id": account_id, "is_running": True, "queue": [],
            "window_hours": 12, "min_leads": 15, "max_leads": 18,
            "warmup_minutes": 10, "active_start_hour_msk": 8,
            "active_end_hour_msk": 23, "session_started_at": None,
            "session_ends_at": None, "schedule": [], "cooldown_until": None,
            "cooldown_reason": None, "last_action_at": None,
            "last_status": None, "updated_at": None,
        }

    async def _account_exists(account_id):  # noqa: ANN001
        return True

    async def _set_running(account_id, is_running, last_status=None):  # noqa: ANN001
        return None

    async def _touch_last_status(account_id, status):  # noqa: ANN001
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
        ("_touch_last_status", _touch_last_status),
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


class _FakeAutochat:
    """create_session: если задан exc — бросает его, иначе возвращает сессию."""
    def __init__(self, exc):
        self._exc = exc

    async def create_session(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        return {"id": 1}


async def test_cancel_and_wait_swallows_cancelled() -> bool:
    """`_cancel_and_wait` не даёт CancelledError утечь наверх (C2)."""
    async def _inf():
        await asyncio.Event().wait()

    task = asyncio.create_task(_inf())
    await asyncio.sleep(0)
    try:
        await ar._cancel_and_wait(task)
    except BaseException as e:  # noqa: BLE001
        print(f"  FAIL: _cancel_and_wait raised {type(e).__name__}")
        return False
    return task.done()


async def test_stop_no_raise_removes_task() -> bool:
    """stop() отменяет живой loop-task БЕЗ CancelledError наверх и убирает его
    из _tasks (C2)."""
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
        await asyncio.sleep(0)

        try:
            await svc.stop(acc, reason="test")
        except BaseException as e:  # noqa: BLE001
            print(f"  FAIL: stop() raised {type(e).__name__}")
            return False

        if acc in svc._tasks:
            print("  FAIL: task not removed by stop()")
            return False
        return True
    finally:
        _unpatch_db(monkey)


async def test_try_start_dialog_returns_reason() -> bool:
    """C1 (return-value модель): `_try_start_dialog` возвращает
    `needs_operator:<code>` когда автомат надо остановить, и `None` на
    benign/success — без долгоживущего словаря-вердикта."""
    from modules.autochat.errors import (
        SessionExpired as AutoSessionExpired, AlreadyContacted,
    )
    monkey: dict = {}
    _patch_db(monkey)
    try:
        svc = ar.WorkerAutoRunnerService(
            get_wrapper=lambda a: object(), autochat_service=None,
        )
        acc = 999004

        # needs_operator (SESSION_EXPIRED) → возвращается reason
        svc._autochat = _FakeAutochat(AutoSessionExpired("expired"))  # type: ignore
        r = await svc._try_start_dialog(acc, "u1", from_crm=False)
        if not (isinstance(r, str) and r.startswith("needs_operator")):
            print(f"  FAIL: needs_operator must return reason, got {r!r}")
            return False

        # benign ALREADY_CONTACTED → None (пропуск слота, не стоп)
        svc._autochat = _FakeAutochat(AlreadyContacted())  # type: ignore
        r = await svc._try_start_dialog(acc, "u2", from_crm=False)
        if r is not None:
            print(f"  FAIL: ALREADY_CONTACTED must return None, got {r!r}")
            return False

        # success → None
        svc._autochat = _FakeAutochat(None)  # type: ignore
        r = await svc._try_start_dialog(acc, "u3", from_crm=False)
        if r is not None:
            print(f"  FAIL: success must return None, got {r!r}")
            return False
        return True
    finally:
        _unpatch_db(monkey)


async def test_do_self_stop_no_raise() -> bool:
    """_do_self_stop отрабатывает без исключений (гасит task, ставит статус)."""
    monkey: dict = {}
    _patch_db(monkey)
    try:
        svc = ar.WorkerAutoRunnerService(
            get_wrapper=lambda a: None, autochat_service=None,
        )
        acc = 999002
        try:
            await svc._do_self_stop(acc, "session_completed")
        except BaseException as e:  # noqa: BLE001
            print(f"  FAIL: _do_self_stop raised {type(e).__name__}")
            return False
        return acc not in svc._tasks
    finally:
        _unpatch_db(monkey)


async def _run_all() -> int:
    results = [
        ("C2: _cancel_and_wait swallows CancelledError",
         await test_cancel_and_wait_swallows_cancelled()),
        ("C2: stop() no raise + removes task",
         await test_stop_no_raise_removes_task()),
        ("C1: _try_start_dialog returns reason / None",
         await test_try_start_dialog_returns_reason()),
        ("C1: _do_self_stop no raise",
         await test_do_self_stop_no_raise()),
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
