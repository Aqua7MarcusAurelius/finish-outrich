"""Smoke-тест выбора промта оценщика (per-account vs глобальный, 2026-07-20).

Проверяем два уровня источника промта lead_evaluator:
  1. `resolve_evaluator_template` (чистая логика приоритета, без БД):
     непустой per-account побеждает; пустой/пробельный → глобальный.
  2. `_load_account_evaluator_template` (чтение из БД):
       - несуществующий account → '' (значит «глобальный по умолчанию»);
       - реальный per-account override читается как записан.

Тест на override делается ВНУТРИ транзакции с ROLLBACK — прод-строка
`account_prompts` не изменяется даже при падении. Живой Postgres нужен.

Запуск:
    docker compose exec -T app python -m tests.smoke.test_evaluator_prompt_selection
"""
from __future__ import annotations

import asyncio
import sys

from core import db as db_mod

from modules.lead_evaluator.prompts import (
    _load_account_evaluator_template,
    resolve_evaluator_template,
)

SENTINEL = "SMOKE_EVAL_OVERRIDE_zzz <verdict>success</verdict>"
GLOBAL = "GLOBAL_EVALUATOR_PROMPT"
MISSING_ACCOUNT_ID = 2_000_000_000  # заведомо не существует


# ─── 1. Чистая логика приоритета (без БД) ────────────────────────────

def test_resolve_prefers_nonempty_account() -> bool:
    ok = resolve_evaluator_template("custom", GLOBAL) == "custom"
    if not ok:
        print("  FAIL: непустой per-account должен побеждать глобальный")
    return ok


def test_resolve_falls_back_on_empty() -> bool:
    cases = ["", "   ", "\n\t "]
    ok = all(resolve_evaluator_template(c, GLOBAL) == GLOBAL for c in cases)
    if not ok:
        print("  FAIL: пустой/пробельный per-account должен давать глобальный")
    return ok


# ─── 2. Чтение из БД ─────────────────────────────────────────────────

async def test_loader_empty_for_missing_row() -> bool:
    pool = db_mod.get_pool()
    async with pool.acquire() as conn:
        loaded = await _load_account_evaluator_template(conn, MISSING_ACCOUNT_ID)
    ok = (loaded == "")
    if not ok:
        print(f"  FAIL: несуществующий account дал {loaded!r}, ждём '' ")
    return ok


async def test_loader_reads_per_account_override() -> bool:
    pool = db_mod.get_pool()
    async with pool.acquire() as conn:
        acc_id = await conn.fetchval("SELECT id FROM accounts ORDER BY id LIMIT 1")
        if acc_id is None:
            print("  SKIP: в БД нет аккаунтов — нечего оверрайдить")
            return True

        # Внутри транзакции с ROLLBACK — прод не трогаем.
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute(
                """
                INSERT INTO account_prompts (
                    account_id, initial_template, reply_template,
                    evaluator_template, updated_at
                ) VALUES ($1, '', '', $2, NOW())
                ON CONFLICT (account_id) DO UPDATE SET
                    evaluator_template = EXCLUDED.evaluator_template
                """,
                acc_id, SENTINEL,
            )
            loaded = await _load_account_evaluator_template(conn, acc_id)
        finally:
            await tr.rollback()

    ok = (loaded == SENTINEL)
    if not ok:
        print(f"  FAIL: per-account override прочитан как {loaded!r}, ждём sentinel")
    return ok


async def test_loader_strips_whitespace_only() -> bool:
    """Пробельный per-account → loader вернёт '' → потом глобальный."""
    pool = db_mod.get_pool()
    async with pool.acquire() as conn:
        acc_id = await conn.fetchval("SELECT id FROM accounts ORDER BY id LIMIT 1")
        if acc_id is None:
            print("  SKIP: в БД нет аккаунтов")
            return True
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute(
                """
                INSERT INTO account_prompts (
                    account_id, initial_template, reply_template,
                    evaluator_template, updated_at
                ) VALUES ($1, '', '', $2, NOW())
                ON CONFLICT (account_id) DO UPDATE SET
                    evaluator_template = EXCLUDED.evaluator_template
                """,
                acc_id, "   \n\t  ",
            )
            loaded = await _load_account_evaluator_template(conn, acc_id)
            resolved = resolve_evaluator_template(loaded, GLOBAL)
        finally:
            await tr.rollback()

    ok = (loaded == "") and (resolved == GLOBAL)
    if not ok:
        print(f"  FAIL: пробельный override дал loaded={loaded!r}, resolved={resolved!r}")
    return ok


async def _run_all() -> int:
    await db_mod.init_pool()
    try:
        results = [
            ("resolve: непустой per-account побеждает",
             test_resolve_prefers_nonempty_account()),
            ("resolve: пустой/пробельный → глобальный",
             test_resolve_falls_back_on_empty()),
            ("loader: несуществующий account → ''",
             await test_loader_empty_for_missing_row()),
            ("loader: per-account override читается (tx rollback)",
             await test_loader_reads_per_account_override()),
            ("loader: пробельный override → '' → глобальный",
             await test_loader_strips_whitespace_only()),
        ]
    finally:
        await db_mod.close_pool()

    print()
    for name, ok in results:
        print(f"  {name:52} → {'PASS' if ok else 'FAIL'}")
    passed = sum(1 for _, ok in results if ok)
    print()
    if passed == len(results):
        print(f"ALL {len(results)} EVALUATOR-PROMPT-SELECTION TESTS PASSED")
        return 0
    print(f"FAILED: {len(results) - passed}/{len(results)}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all()))
