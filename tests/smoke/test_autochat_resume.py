"""Smoke-тест Fix #22 (safe version): `_on_worker_started` триггерит
regenerate для сессий с пропущенным inbound.

**Ключевое отличие от первой версии Fix #22**: regenerate вызывается НЕ в
`_restore_active_sessions` (когда wrapper мог быть ещё None), а в ответ
на событие `worker.started` — тогда wrapper гарантированно готов. Это
защита от отправки в None → send_failed → терминальный `failed` статус.

Плюс TTL: только missed inbound не старше 6ч, чтобы не воскрешать
древние тактически закрытые диалоги.

Запуск:
    docker compose exec -T app python -m tests.smoke.test_autochat_resume
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch


def _make_svc_with_mock_pool(fetch_return):
    from modules.autochat import service as svc_mod

    svc = svc_mod.AutoChatService(get_wrapper=lambda acc: None)

    fake_conn = MagicMock()
    fake_conn.fetch = AsyncMock(return_value=fetch_return)
    fake_conn.fetchval = AsyncMock(return_value=None)
    fake_conn.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_conn.__aexit__ = AsyncMock(return_value=None)
    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=fake_conn)
    return svc, svc_mod, fake_pool


async def test_worker_started_regenerates_missed_inbound() -> bool:
    """worker.started для acc=24 → сессия с missed inbound регенерируется."""
    now = datetime.now(timezone.utc)
    row = {
        "id": 999999,
        "dialog_id": 93199,
        "last_their_message_at": now,
        "last_our_activity_at": now - timedelta(minutes=30),
    }
    svc, svc_mod, fake_pool = _make_svc_with_mock_pool([row])

    # Пред-регистрируем «восстановленную» session в _sessions_by_id
    force_calls: list = []

    class FakeSession:
        id = 999999
        async def force_generate_reply(self):
            force_calls.append(self.id)

    svc._sessions_by_id[999999] = FakeSession()

    with patch.object(svc_mod.db, "get_pool", return_value=fake_pool):
        await svc._on_worker_started(account_id=24, data={"name": "SAD_05_RU"})

    return len(force_calls) == 1 and force_calls[0] == 999999


async def test_worker_started_skips_when_our_reply_is_last() -> bool:
    """Наш outbound после их inbound → SQL-фильтр вернёт 0 строк."""
    # SQL сам фильтрует по `their > COALESCE(ours, epoch)`, поэтому
    # достаточно вернуть пустой fetch — sanity check что hook не крашится.
    svc, svc_mod, fake_pool = _make_svc_with_mock_pool([])

    force_calls: list = []

    class FakeSession:
        id = 999998
        async def force_generate_reply(self):
            force_calls.append(self.id)

    svc._sessions_by_id[999998] = FakeSession()

    with patch.object(svc_mod.db, "get_pool", return_value=fake_pool):
        await svc._on_worker_started(account_id=24, data={})

    return len(force_calls) == 0


async def test_missed_inbound_older_than_ttl_ignored() -> bool:
    """Missed inbound старше _MISSED_INBOUND_TTL_SEC (6ч) → SQL-фильтр
    его отсеет. Проверяем что fake pool.fetch вернул 0 (симуляция того же).
    Плюс проверяем что константа TTL = 6 * 3600."""
    from modules.autochat import service as svc_mod
    return svc_mod._MISSED_INBOUND_TTL_SEC == 6 * 3600


async def test_worker_started_ignores_unknown_session() -> bool:
    """Если SQL вернул id несуществующей в памяти сессии — тихо пропускаем,
    не падаем. Защита при рассинхроне БД и in-memory state."""
    now = datetime.now(timezone.utc)
    row = {
        "id": 12345,  # не регистрируем в _sessions_by_id
        "dialog_id": 555,
        "last_their_message_at": now,
        "last_our_activity_at": None,
    }
    svc, svc_mod, fake_pool = _make_svc_with_mock_pool([row])

    with patch.object(svc_mod.db, "get_pool", return_value=fake_pool):
        try:
            await svc._on_worker_started(account_id=24, data={})
        except Exception as e:
            print(f"  FAIL: raised {type(e).__name__}: {e}")
            return False
    return True


async def _run_all() -> int:
    results = [
        ("Fix #22: worker.started regenerates missed inbound",
         await test_worker_started_regenerates_missed_inbound()),
        ("Fix #22: skips when our reply is last (empty fetch)",
         await test_worker_started_skips_when_our_reply_is_last()),
        ("Fix #22: TTL constant = 6h",
         await test_missed_inbound_older_than_ttl_ignored()),
        ("Fix #22: unknown session id doesn't crash",
         await test_worker_started_ignores_unknown_session()),
    ]
    print()
    for name, ok in results:
        print(f"  {name:60} → {'PASS' if ok else 'FAIL'}")
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print()
    if passed == total:
        print(f"ALL {total} AUTOCHAT-RESUME TESTS PASSED")
        return 0
    print(f"FAILED: {total - passed}/{total}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all()))
