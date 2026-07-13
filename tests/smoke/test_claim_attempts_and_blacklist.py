"""Smoke-тесты пула лидов (обновлено 2026-07-13 под простую модель).

Простая модель (решение владельца): claim СРАЗУ удаляет лида из пула, reinsert
возвращает обратно. Никаких claimed_at/attempts/reaper и никакого авто-invalid
у партнёра — invalid наружу только по реальному «нельзя написать» (ветка
invalid_lead в auto_runner → push_lead_finalized). Дедуп «никогда повторно»
держит create_session по telegram_user_id (см. test_contacted_dedup).

  Fix #14 (актуален): таблица invalid_leads наполняется ТОЛЬКО реальными invalid
  (push_lead_finalized при status=invalid); poller (_take_into_pool) их игнорирует.

Юзер `test_zephyr_lead_ATTEMPTS` — тестовый, чистится в начале/конце.

Запуск:
    docker compose exec -T app python -m tests.smoke.test_claim_attempts_and_blacklist
"""
from __future__ import annotations

import asyncio
import sys

from core import bus as bus_mod
from core import db as db_mod


async def _noop_publish(*args, **kwargs) -> None:
    return None


TEST_USER = "test_zephyr_lead_ATTEMPTS"


async def _cleanup(conn) -> None:
    await conn.execute("DELETE FROM crm_lead_pool WHERE username = $1", TEST_USER)
    await conn.execute("DELETE FROM invalid_leads WHERE username = $1", TEST_USER)
    await conn.execute("DELETE FROM contacted_leads WHERE lower(username) = lower($1)", TEST_USER)


async def test_claim_deletes_and_reinsert_readds() -> bool:
    """Простая модель: claim УДАЛЯЕТ лида из пула; reinsert возвращает обратно;
    сколько бы раз ни реинзертили — никакого invalid/push у партнёра."""
    from modules.partner_crm.service import PartnerCrmService
    pool = db_mod.get_pool()
    async with pool.acquire() as conn:
        await _cleanup(conn)
        # fetched_at в далёком прошлом → claim (ORDER BY fetched_at ASC) заберёт
        # именно нашего тест-юзера, а не свежих продовых лидов.
        await conn.execute(
            "INSERT INTO crm_lead_pool (username, gender, fetched_at) "
            "VALUES ($1, 'female', '1970-01-01'::timestamptz)",
            TEST_USER,
        )

    svc = PartnerCrmService()

    # push_lead_finalized НЕ должен вызваться ни разу (нет авто-invalid).
    calls: list = []
    orig = svc.push_lead_finalized

    async def _capture_push(**kw):
        calls.append(kw)

    svc.push_lead_finalized = _capture_push  # type: ignore

    async def in_pool() -> bool:
        async with pool.acquire() as c:
            return bool(await c.fetchval(
                "SELECT 1 FROM crm_lead_pool WHERE username = $1", TEST_USER))

    try:
        # 1) claim → лид ИСЧЕЗ из пула (DELETE-модель).
        claimed = await svc.claim_lead_for_account(account_id=23, target_gender="any")
        if claimed is None or claimed["username"] != TEST_USER:
            print("  FAIL: claim did not return the test lead"); return False
        if await in_pool():
            print("  FAIL: after claim, lead must be DELETEd from pool"); return False

        # 2) reinsert → лид снова в пуле, вернул True.
        kept = await svc.reinsert_into_pool(
            account_id=23, username=TEST_USER, gender="female",
        )
        if not kept:
            print("  FAIL: reinsert should return True"); return False
        if not await in_pool():
            print("  FAIL: after reinsert, lead must be back in pool"); return False

        # 3) Реинзертим ещё несколько раз (идемпотентно) — лид остаётся, push
        #    не вызывается, в чёрный список не попадает. (Не переклеймиваем,
        #    чтобы не задеть продовые лиды.)
        for _ in range(4):
            kept = await svc.reinsert_into_pool(
                account_id=23, username=TEST_USER, gender="female",
            )
            if not kept:
                print("  FAIL: repeated reinsert should keep returning True"); return False

        async with pool.acquire() as c:
            still = await c.fetchval(
                "SELECT 1 FROM crm_lead_pool WHERE username = $1", TEST_USER)
            black = await c.fetchval(
                "SELECT 1 FROM invalid_leads WHERE username = $1", TEST_USER)
        if still is None:
            print("  FAIL: lead must stay in pool after repeated reinserts"); return False
        if black is not None:
            print("  FAIL: lead must NOT be blacklisted (no penalty for our failures)"); return False
        if len(calls) != 0:
            print(f"  FAIL: push_lead_finalized must NOT be called, got {calls}"); return False
    finally:
        svc.push_lead_finalized = orig  # type: ignore
        async with pool.acquire() as c:
            await _cleanup(c)
    return True


async def test_take_into_pool_skips_blacklisted() -> bool:
    """Fix #14: _take_into_pool → 'skipped_invalid' если username в invalid_leads."""
    from modules.partner_crm.service import PartnerCrmService
    pool = db_mod.get_pool()
    async with pool.acquire() as conn:
        await _cleanup(conn)
        # Пред-занесённый чёрный список
        await conn.execute(
            "INSERT INTO invalid_leads (username, reason) VALUES ($1, 'test-preseed')",
            TEST_USER,
        )

    svc = PartnerCrmService()
    # Мокаем CRM client чтобы set_lead_status не вызывался
    class _FakeCrm:
        async def set_lead_status(self, uname, status):
            raise AssertionError(
                f"CRM should not be called for blacklisted user {uname}"
            )
    svc._client = _FakeCrm()  # type: ignore

    result = await svc._take_into_pool(
        {"username": TEST_USER, "gender": "female"}, origin="assigned",
    )
    ok = result == "skipped_invalid"
    if not ok:
        print(f"  FAIL: expected 'skipped_invalid', got {result!r}")

    async with pool.acquire() as c:
        await _cleanup(c)
    return ok


async def _run_all() -> int:
    await db_mod.init_pool()
    # Redis для тестов не поднимаем — мокаем bus.publish, чтобы claim
    # и reinsert могли работать без Redis.
    orig_publish = bus_mod.publish
    bus_mod.publish = _noop_publish  # type: ignore
    try:
        results = [
            ("claim DELETEs + reinsert re-adds, no auto-invalid",
             await test_claim_deletes_and_reinsert_readds()),
            ("Fix #14 _take_into_pool skips blacklisted username",
             await test_take_into_pool_skips_blacklisted()),
        ]
    finally:
        bus_mod.publish = orig_publish  # type: ignore
        await db_mod.close_pool()
    print()
    for name, ok in results:
        print(f"  {name:60} → {'PASS' if ok else 'FAIL'}")
    passed = sum(1 for _, ok in results if ok)
    print()
    if passed == len(results):
        print(f"ALL {len(results)} TESTS PASSED")
        return 0
    print(f"FAILED: {len(results) - passed}/{len(results)}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all()))
