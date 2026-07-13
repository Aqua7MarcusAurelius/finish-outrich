"""Smoke-тест кросс-аккаунт дедупа (2026-07-13).

Реестр `contacted_leads` + дедуп: прописали человека раз → больше НИКОГДА,
любым аккаунтом. Проверяем два места отсева:
  1. `_take_into_pool` (рефилл) → 'skipped_contacted' для контактированного.
  2. claim-исключение: `NOT EXISTS contacted_leads` реально прячет лида из
     выборки claim (проверяем зеркальным SELECT'ом — без побочек на проде).

Тест-юзер чистится в начале и конце, прод-данные не трогает.

Запуск:
    docker compose exec -T app python -m tests.smoke.test_contacted_dedup
"""
from __future__ import annotations

import asyncio
import sys

from core import bus as bus_mod
from core import db as db_mod


async def _noop_publish(*args, **kwargs) -> None:
    return None


TEST_USER = "test_contacted_dedup_zzz"

# Зеркало WHERE из claim_lead_for_account (простая модель: все строки пула
# свободны, claim удаляет забранного; исключаем уже-контактированных по
# username). Если строка возвращается — лид claim'абелен.
_CLAIM_MIRROR = """
    SELECT id FROM crm_lead_pool
    WHERE lower(username) = lower($1)
      AND NOT EXISTS (
            SELECT 1 FROM contacted_leads c
            WHERE lower(c.username) = lower(crm_lead_pool.username)
      )
    LIMIT 1
"""


async def _cleanup(conn) -> None:
    await conn.execute("DELETE FROM crm_lead_pool WHERE lower(username) = lower($1)", TEST_USER)
    await conn.execute("DELETE FROM contacted_leads WHERE lower(username) = lower($1)", TEST_USER)


async def test_take_into_pool_skips_contacted() -> bool:
    from modules.partner_crm.service import PartnerCrmService
    pool = db_mod.get_pool()
    async with pool.acquire() as conn:
        await _cleanup(conn)
        await conn.execute(
            "INSERT INTO contacted_leads (username, telegram_user_id, account_id) "
            "VALUES ($1, 111, 23)",
            TEST_USER,
        )
    svc = PartnerCrmService()
    r = await svc._take_into_pool({"username": TEST_USER, "gender": "female"}, origin="test")
    ok = (r == "skipped_contacted")
    if not ok:
        print(f"  FAIL: _take_into_pool returned {r!r}, expected 'skipped_contacted'")
    async with pool.acquire() as conn:
        await _cleanup(conn)
    return ok


async def test_claim_excludes_contacted() -> bool:
    pool = db_mod.get_pool()
    async with pool.acquire() as conn:
        await _cleanup(conn)
        # Лид в пуле, свободен, ещё НЕ контактирован → claim-зеркало его видит.
        await conn.execute(
            "INSERT INTO crm_lead_pool (username, gender, fetched_at) "
            "VALUES ($1, 'female', '1970-01-01'::timestamptz)",
            TEST_USER,
        )
        visible_before = await conn.fetchval(_CLAIM_MIRROR, TEST_USER)
        # Помечаем контактированным → claim-зеркало его прячет.
        await conn.execute(
            "INSERT INTO contacted_leads (username, telegram_user_id, account_id) "
            "VALUES ($1, 111, 23) ON CONFLICT DO NOTHING",
            TEST_USER,
        )
        visible_after = await conn.fetchval(_CLAIM_MIRROR, TEST_USER)
        await _cleanup(conn)

    ok = (visible_before is not None) and (visible_after is None)
    if not ok:
        print(f"  FAIL: visible_before={visible_before} (want not None), "
              f"visible_after={visible_after} (want None)")
    return ok


async def _run_all() -> int:
    await db_mod.init_pool()
    orig_publish = bus_mod.publish
    bus_mod.publish = _noop_publish  # type: ignore
    try:
        results = [
            ("refill _take_into_pool skips contacted lead",
             await test_take_into_pool_skips_contacted()),
            ("claim excludes contacted lead (NOT EXISTS)",
             await test_claim_excludes_contacted()),
        ]
    finally:
        bus_mod.publish = orig_publish  # type: ignore
        await db_mod.close_pool()
    print()
    for name, ok in results:
        print(f"  {name:55} → {'PASS' if ok else 'FAIL'}")
    passed = sum(1 for _, ok in results if ok)
    print()
    if passed == len(results):
        print(f"ALL {len(results)} CONTACTED-DEDUP TESTS PASSED")
        return 0
    print(f"FAILED: {len(results) - passed}/{len(results)}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all()))
