"""Smoke-тест обновления last_status при успешной отправке лида (Fix #3).

Проверяем что вызов ``_touch_last_status`` действительно перезаписывает
устаревшую ошибочную пометку. Использует реальный pool из db.get_pool()
на тестовом account_id.

Запуск (внутри контейнера app):
    docker compose exec -T app python -m tests.smoke.test_last_status_refresh
"""
from __future__ import annotations

import asyncio
import sys

from core import db as db_mod


async def _run() -> int:
    from modules.worker_auto_runner.service import (
        _touch_last_status,
        _ensure_row,
    )

    # Ищем какой-нибудь существующий воркер-аккаунт для теста —
    # трогать accounts._id мы не будем, только worker_auto_runners.
    pool = db_mod.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM accounts WHERE id BETWEEN 23 AND 48 LIMIT 1"
        )
    if row is None:
        print("SKIP: no accounts in range 23-48 available, test skipped")
        return 0
    account_id = int(row["id"])

    # Ставим устаревшую ошибочную метку.
    await _ensure_row(account_id)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE worker_auto_runners
            SET last_status = 'stopped (needs_operator:SESSION_EXPIRED)'
            WHERE account_id = $1
            """,
            account_id,
        )
        stale = await conn.fetchval(
            "SELECT last_status FROM worker_auto_runners WHERE account_id = $1",
            account_id,
        )
    if stale != "stopped (needs_operator:SESSION_EXPIRED)":
        print(f"SETUP FAILED: expected stale, got {stale!r}")
        return 1

    # Действие: touch_last_status как в _try_start_dialog при lead_sent.
    await _touch_last_status(account_id, "running")

    async with pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT last_status FROM worker_auto_runners WHERE account_id = $1",
            account_id,
        )
    if after != "running":
        print(f"FAIL: expected 'running' after touch, got {after!r}")
        return 1

    print(f"PASS: account_id={account_id} refreshed last_status stale → 'running'")

    # Восстановим "stopped (manual)" чтобы не оставлять грязных изменений.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE worker_auto_runners SET last_status = 'stopped (manual)' WHERE account_id = $1",
            account_id,
        )
    return 0


async def main() -> int:
    await db_mod.init_pool()
    try:
        return await _run()
    finally:
        await db_mod.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
