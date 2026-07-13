"""Smoke-тесты Fix #7 / Fix #8 / Fix #9.

  * Fix #7: "user was deleted" в тексте `RPCError` → раисит `CannotWrite`
    (проверяем что маркер добавлен в `_RPC_CANNOT_WRITE_MARKERS`).
  * reinsert (простая модель 2026-07-13): просто вставляет лида обратно в пул,
    возвращает True и НИКОГДА не пушит invalid партнёру (attempts убраны).
  * Fix #9: `_msk_wall_clock_window` и `_generate_schedule` строят
    расписание в границах активного окна МСК.

Запуск (внутри контейнера app):
    docker compose exec -T app python -m tests.smoke.test_wall_clock_and_retry
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone


def test_user_deleted_marker_present() -> bool:
    from modules.autochat.service import _RPC_CANNOT_WRITE_MARKERS
    return "user was deleted" in _RPC_CANNOT_WRITE_MARKERS


def test_user_deleted_matches_actual_text() -> bool:
    from modules.autochat.service import _RPC_CANNOT_WRITE_MARKERS
    real_msg = "The specified user was deleted (caused by SendMessageRequest)"
    return any(m in real_msg for m in _RPC_CANNOT_WRITE_MARKERS)


def test_wall_clock_today_window() -> bool:
    """Утро (08:00 MSK = 05:00 UTC), окно 8-23 MSK → сегодняшнее окно."""
    from modules.worker_auto_runner.service import _msk_wall_clock_window
    now_utc = datetime(2026, 7, 11, 5, 0, tzinfo=timezone.utc)  # 08:00 MSK
    start, end = _msk_wall_clock_window(
        now_utc, active_start_hour_msk=8, active_end_hour_msk=23,
    )
    # start = 08:00 MSK = 05:00 UTC, end = 23:00 MSK = 20:00 UTC
    expected_start = datetime(2026, 7, 11, 5, 0, tzinfo=timezone.utc)
    expected_end = datetime(2026, 7, 11, 20, 0, tzinfo=timezone.utc)
    return start == expected_start and end == expected_end


def test_wall_clock_late_evening_rolls_to_tomorrow() -> bool:
    """23:30 MSK — уже позже конца окна → окно на завтра."""
    from modules.worker_auto_runner.service import _msk_wall_clock_window
    now_utc = datetime(2026, 7, 11, 20, 30, tzinfo=timezone.utc)  # 23:30 MSK
    start, end = _msk_wall_clock_window(
        now_utc, active_start_hour_msk=8, active_end_hour_msk=23,
    )
    expected_start = datetime(2026, 7, 12, 5, 0, tzinfo=timezone.utc)
    expected_end = datetime(2026, 7, 12, 20, 0, tzinfo=timezone.utc)
    return start == expected_start and end == expected_end


def test_generate_schedule_stays_in_window() -> bool:
    """Все точки расписания в окне активной прописи."""
    from modules.worker_auto_runner.service import _generate_schedule
    now_utc = datetime(2026, 7, 11, 6, 0, tzinfo=timezone.utc)  # 09:00 MSK
    schedule, ends = _generate_schedule(
        now_utc,
        active_start_hour_msk=8, active_end_hour_msk=23,
        min_leads=15, max_leads=18, warmup_minutes=10,
    )
    if not schedule:
        return False
    if len(schedule) < 15 or len(schedule) > 18:
        return False
    # Все — до ends
    if any(ts > ends for ts in schedule):
        return False
    # Все — после now+warmup
    warmup_end = now_utc + timedelta(minutes=10)
    if any(ts < warmup_end for ts in schedule):
        return False
    # Отсортированы
    if schedule != sorted(schedule):
        return False
    return True


class _FakePool:
    """Мини-двойник asyncpg pool для теста простого reinsert (2026-07-13)."""

    def __init__(self) -> None:
        self.inserted = False
        self.calls: list[str] = []

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_ctx):
                return _FakeConn(pool)

            async def __aexit__(self_ctx, exc_type, exc, tb):
                return False

        return _Ctx()


class _FakeConn:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def execute(self, sql: str, *args):
        if "INSERT INTO crm_lead_pool" in sql:
            self._pool.inserted = True
        self._pool.calls.append(sql[:60])


class _FakePushClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def push_lead_finalized(self, **kwargs):
        self.calls.append(kwargs)


async def test_reinsert_readds_no_push() -> bool:
    """Простая модель (2026-07-13): reinsert просто вставляет лида обратно в пул
    (INSERT ON CONFLICT), возвращает True и НИКОГДА не пушит invalid партнёру —
    вне зависимости от числа попыток (attempts как понятия больше нет)."""
    from modules.partner_crm import service as pcs_mod
    from modules.partner_crm.service import PartnerCrmService

    fake_pool = _FakePool()
    real_get_pool = pcs_mod.db.get_pool
    pcs_mod.db.get_pool = lambda: fake_pool  # type: ignore

    try:
        svc = PartnerCrmService()
        push_client = _FakePushClient()
        svc.push_lead_finalized = push_client.push_lead_finalized  # type: ignore

        # Реинзертим несколько раз — всегда True, всегда INSERT, никакого push.
        for _ in range(5):
            result = await svc.reinsert_into_pool(
                account_id=42, username="test_user", gender="female",
            )
            if result is not True:
                return False
        return fake_pool.inserted and len(push_client.calls) == 0
    finally:
        pcs_mod.db.get_pool = real_get_pool  # type: ignore


async def _run_all() -> int:
    results = [
        ("Fix #7 user_deleted marker present",
         test_user_deleted_marker_present()),
        ("Fix #7 marker matches real TG message",
         test_user_deleted_matches_actual_text()),
        ("Fix #9 wall-clock TODAY window",
         test_wall_clock_today_window()),
        ("Fix #9 wall-clock rolls to tomorrow",
         test_wall_clock_late_evening_rolls_to_tomorrow()),
        ("Fix #9 generate_schedule stays in window",
         test_generate_schedule_stays_in_window()),
        ("reinsert re-adds to pool, returns True, NO partner push",
         await test_reinsert_readds_no_push()),
    ]
    print()
    for name, ok in results:
        print(f"  {name:48} → {'PASS' if ok else 'FAIL'}")
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print()
    if passed == total:
        print(f"ALL {total} TESTS PASSED")
        return 0
    print(f"FAILED: {total - passed}/{total}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all()))
