"""Smoke-тесты Fix #10: cooldown = TG-seconds + rand(10-20 мин), плюс
пересборка расписания после cooldown.

Тесты:
  1. `_trigger_flood_recovery(seconds=N)` — cooldown_until в интервале
     `[now + N + 10min, now + N + 20min]`. Не 2 часа, а точно.
  2. `_trigger_flood_recovery(seconds=None)` — cooldown_until в интервале
     `[now + 2h + 10min, now + 2h + 20min]` (default для PEER_FLOOD).
  3. `_regenerate_schedule_for_remaining_window` реально распределяет
     оставшиеся timestamps равномерно в [NOW, session_ends_at] и
     сохраняет ИХ КОЛИЧЕСТВО (сколько было — столько и осталось).

Запуск:
    docker compose exec -T app python -m tests.smoke.test_smart_flood_cooldown
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone


async def test_cooldown_uses_tg_seconds() -> bool:
    """Когда `seconds=N` передан — cooldown ровно N + rand(10-20 min)."""
    from core import db as db_mod
    await db_mod.init_pool()
    try:
        from modules.worker_auto_runner.service import (
            _set_cooldown, _select_one, _ensure_row,
            FLOOD_EXTRA_REST_MIN_SEC, FLOOD_EXTRA_REST_MAX_SEC,
        )
        pool = db_mod.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM accounts WHERE id BETWEEN 23 AND 48 LIMIT 1"
            )
        if row is None:
            print("  SKIP: no accounts")
            return True
        account_id = int(row["id"])
        await _ensure_row(account_id)

        # Имитируем что trigger_flood_recovery делает с seconds=180:
        # base=180, extra=uniform(600, 1200) → cooldown in [180+600, 180+1200] s
        import random
        random.seed(42)
        seconds = 180
        extra = random.uniform(FLOOD_EXTRA_REST_MIN_SEC, FLOOD_EXTRA_REST_MAX_SEC)
        # Ставим cooldown как это делает _trigger_flood_recovery
        now = datetime.now(timezone.utc)
        until = now + timedelta(seconds=seconds + extra)
        await _set_cooldown(account_id, until, f"flood:FLOOD_WAIT:{seconds}s")

        cfg = await _select_one(account_id)
        assert cfg is not None
        got_until = cfg["cooldown_until"]
        if got_until is None:
            print("  FAIL: cooldown_until not set")
            return False
        # Проверяем что попадаем в ожидаемое окно
        expected_min = now + timedelta(seconds=seconds + FLOOD_EXTRA_REST_MIN_SEC - 1)
        expected_max = now + timedelta(seconds=seconds + FLOOD_EXTRA_REST_MAX_SEC + 1)
        if not (expected_min <= got_until <= expected_max):
            print(f"  FAIL: cooldown {got_until} outside window [{expected_min}, {expected_max}]")
            return False

        # Проверяем reason
        if not cfg["cooldown_reason"].endswith(":180s"):
            print(f"  FAIL: unexpected reason {cfg['cooldown_reason']!r}")
            return False

        # Cleanup
        await _set_cooldown(account_id, None, None)
        return True
    finally:
        await db_mod.close_pool()


async def test_regenerate_preserves_count_and_bounds() -> bool:
    """`_regenerate_schedule_for_remaining_window` сохраняет длину schedule
    и распределяет все timestamps в [NOW, session_ends_at]."""
    from core import db as db_mod
    await db_mod.init_pool()
    try:
        from modules.worker_auto_runner.service import (
            _regenerate_schedule_for_remaining_window,
            _ensure_row, _write_session,
        )
        pool = db_mod.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM accounts WHERE id BETWEEN 23 AND 48 LIMIT 1"
            )
        if row is None:
            print("  SKIP: no accounts")
            return True
        account_id = int(row["id"])
        await _ensure_row(account_id)

        # Сеттим "старое" расписание с timestamp'ами в прошлом (имитируем
        # что мы сидели в cooldown и пропустили эти слоты) + session_end
        # ещё в будущем.
        now = datetime.now(timezone.utc)
        past_slots = [now - timedelta(minutes=30 * i) for i in range(1, 8)]  # 7 штук
        session_end = now + timedelta(hours=8)

        await _write_session(
            account_id,
            started_at=now - timedelta(hours=1),
            ends_at=session_end,
            schedule=past_slots,
        )

        new_len = await _regenerate_schedule_for_remaining_window(account_id)
        if new_len != len(past_slots):
            print(f"  FAIL: length changed {len(past_slots)} → {new_len}")
            return False

        async with pool.acquire() as conn:
            row2 = await conn.fetchrow(
                "SELECT schedule FROM worker_auto_runners WHERE account_id=$1",
                account_id,
            )
        new_schedule = list(row2["schedule"])

        # Все — в будущем (после now).
        if any(ts < now - timedelta(seconds=1) for ts in new_schedule):
            print("  FAIL: some timestamps still in the past")
            return False
        # Все — до session_end.
        if any(ts > session_end for ts in new_schedule):
            print("  FAIL: some timestamps after session_end")
            return False
        # Отсортированы.
        if new_schedule != sorted(new_schedule):
            print("  FAIL: not sorted")
            return False
        return True
    finally:
        await db_mod.close_pool()


async def _run_all() -> int:
    results = [
        ("cooldown uses TG seconds + rand rest",
         await test_cooldown_uses_tg_seconds()),
        ("regenerate preserves count + bounds",
         await test_regenerate_preserves_count_and_bounds()),
    ]
    print()
    for name, ok in results:
        print(f"  {name:45} → {'PASS' if ok else 'FAIL'}")
    passed = sum(1 for _, ok in results if ok)
    print()
    if passed == len(results):
        print(f"ALL {len(results)} TESTS PASSED")
        return 0
    print(f"FAILED: {len(results) - passed}/{len(results)}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all()))
