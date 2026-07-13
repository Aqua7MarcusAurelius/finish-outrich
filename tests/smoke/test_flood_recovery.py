"""Smoke-тест Fix #5: детектор PEER_FLOOD / FloodWait + cooldown.

Юнит-кейсы на чистую функцию ``_is_flood_error`` (pure).
Плюс интеграционный кейс: `_set_cooldown` реально пишет поля в БД и они
читаются через `_select_one`.

Запуск (внутри контейнера app):
    docker compose exec -T app python -m tests.smoke.test_flood_recovery
"""
from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

from core import db as db_mod
from modules.worker_auto_runner.service import (
    WorkerAutoRunnerService,
    _set_cooldown,
    _select_one,
    _ensure_row,
    _utcnow,
    FLOOD_COOLDOWN_BASE_SEC,
    SPAM_BOT_START_ATTEMPTS,
    SPAM_BOT_USERNAME,
)


def test_is_flood_matrix() -> list[tuple[str, str, bool, bool]]:
    """(err_code, err_msg, expected, ok)"""
    cases: list[tuple[str, str, bool]] = [
        # PEER_FLOOD → должно матчиться
        ("SEND_FAILED_TRANSIENT", "Too many requests (caused by SendMessageRequest)", True),
        ("SEND_FAILED_TRANSIENT", "Too many requests", True),
        # FloodWait с явными секундами → должно
        ("FLOOD_WAIT", "flood wait 30 seconds", True),
        ("FloodWaitError", "A wait of 60 seconds is required", True),
        # SEND_FAILED_TRANSIENT с ДРУГИМ текстом (не flood) → НЕ должно
        ("SEND_FAILED_TRANSIENT", "The specified user was deleted (caused by SendMessageRequest)", False),
        ("SEND_FAILED_TRANSIENT", "some other transient error", False),
        # invalid_lead коды → не flood
        ("CANNOT_WRITE", "ALLOW_PAYMENT_REQUIRED", False),
        ("USERNAME_NOT_FOUND", "not found", False),
        # needs_operator коды → не flood
        ("SESSION_EXPIRED", "session revoked", False),
        # Неизвестный код → не flood
        ("UNKNOWN", "Too many requests", False),
    ]
    return [
        (code, msg, expected, WorkerAutoRunnerService._is_flood_error(code, msg) == expected)
        for code, msg, expected in cases
    ]


def test_constants_sanity() -> bool:
    """Sanity-check на константы Fix #5 + Fix #11 (2026-07-12: base=15мин)."""
    return (
        FLOOD_COOLDOWN_BASE_SEC == 15 * 60
        and SPAM_BOT_START_ATTEMPTS == 2
        and SPAM_BOT_USERNAME == "SpamBot"
    )


async def test_cooldown_persistence() -> bool:
    """`_set_cooldown` пишет в БД, `_select_one` возвращает те же значения."""
    pool = db_mod.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM accounts WHERE id BETWEEN 23 AND 48 LIMIT 1"
        )
    if row is None:
        print("  SKIP: no accounts in range 23-48")
        return True
    account_id = int(row["id"])
    await _ensure_row(account_id)

    # Ставим cooldown
    until = _utcnow() + timedelta(seconds=FLOOD_COOLDOWN_BASE_SEC)
    await _set_cooldown(account_id, until, "flood:SEND_FAILED_TRANSIENT")
    cfg = await _select_one(account_id)
    assert cfg is not None
    got_until = cfg["cooldown_until"]
    got_reason = cfg["cooldown_reason"]
    if got_until is None or got_reason != "flood:SEND_FAILED_TRANSIENT":
        print(f"  FAIL: cooldown not persisted: until={got_until} reason={got_reason}")
        return False

    # Сброс
    await _set_cooldown(account_id, None, None)
    cfg2 = await _select_one(account_id)
    if cfg2["cooldown_until"] is not None or cfg2["cooldown_reason"] is not None:
        print("  FAIL: cooldown not cleared")
        return False
    return True


async def _run_all() -> int:
    print("=== _is_flood_error matrix ===")
    failures = 0
    for code, msg, expected, ok in test_is_flood_matrix():
        mark = "PASS" if ok else "FAIL"
        print(f"  code={code!r:25} expected={expected!s:5} → {mark}")
        if not ok:
            failures += 1

    print()
    if test_constants_sanity():
        print("  Constants sanity  → PASS")
    else:
        print("  Constants sanity  → FAIL")
        failures += 1

    print()
    print("=== Cooldown persistence ===")
    if await test_cooldown_persistence():
        print("  cooldown persistence  → PASS")
    else:
        failures += 1

    print()
    if failures:
        print(f"FAILED: {failures} test(s)")
        return 1
    print("ALL FLOOD-RECOVERY TESTS PASSED")
    return 0


async def main() -> int:
    await db_mod.init_pool()
    try:
        return await _run_all()
    finally:
        await db_mod.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
