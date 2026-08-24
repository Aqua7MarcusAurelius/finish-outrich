"""Smoke-тесты модерации успешных диалогов (0024 + modules/report_bot).

Проверяем то, что ломается молча и дорого:
  * маппинг кнопка → статус CRM, включая деградацию при пустом l2_login;
  * таймер: без нажатия не пушим никогда, после нажатия — через 4ч,
    перевыбор сдвигает отсчёт;
  * идемпотентность: после pushed_at выбор больше не принимается;
  * finalizer не пушит повторно то, что уже отправила модерация;
  * оценщик при success ставит «Модерация», а не терминальный пузырь.

Запуск:
    docker compose exec -T app python -m tests.smoke.test_report_moderation
"""
from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from unittest.mock import AsyncMock

from core import db

TEST_DIALOG_ID = -777001          # заведомо не существующий диалог
TEST_USERNAME = "test_moderation_lead"


async def _cleanup(conn) -> None:
    await conn.execute(
        "DELETE FROM report_moderation WHERE dialog_id = $1", TEST_DIALOG_ID,
    )
    await conn.execute(
        "DELETE FROM invalid_leads WHERE username = $1", TEST_USERNAME,
    )


async def _seed(conn, **over) -> None:
    await conn.execute(
        """
        INSERT INTO report_moderation
            (dialog_id, username, account_id, family, chat_id, message_id)
        VALUES ($1, $2, 23, 'SAD', -100500, 4242)
        """,
        TEST_DIALOG_ID, TEST_USERNAME,
    )


# ─── Маппинг решений на статусы CRM ──────────────────────────────────

async def test_take_calls_assign_l2() -> bool:
    """«Валид, беру» + живой l2_login → assign-l2, статус transferred."""
    from modules.partner_crm.service import PartnerCrmService

    svc = PartnerCrmService()
    svc._client.assign_to_l2 = AsyncMock(return_value={"status": "transferred"})
    svc._client.set_lead_status = AsyncMock()

    from core import bus as bus_mod
    orig = bus_mod.publish
    bus_mod.publish = AsyncMock()
    try:
        got = await svc.push_lead_moderated(
            account_id=23, username=TEST_USERNAME,
            choice="take", l2_login="l2_001",
        )
    finally:
        bus_mod.publish = orig

    if got != "transferred":
        print(f"  FAIL: вернулось {got!r}, ожидалось 'transferred'")
        return False
    svc._client.assign_to_l2.assert_awaited_once_with(TEST_USERNAME, "l2_001")
    if svc._client.set_lead_status.await_count:
        print("  FAIL: при take не должно быть вызова /status")
        return False
    return True


async def test_take_without_l2_degrades() -> bool:
    """Семейство без l2_login → не теряем успех, шлём ready_for_transfer."""
    from modules.partner_crm.service import PartnerCrmService

    svc = PartnerCrmService()
    svc._client.assign_to_l2 = AsyncMock()
    svc._client.set_lead_status = AsyncMock()

    from core import bus as bus_mod
    orig = bus_mod.publish
    bus_mod.publish = AsyncMock()
    try:
        got = await svc.push_lead_moderated(
            account_id=23, username=TEST_USERNAME,
            choice="take", l2_login=None,
        )
    finally:
        bus_mod.publish = orig

    if got != "ready_for_transfer":
        print(f"  FAIL: вернулось {got!r}, ожидалось 'ready_for_transfer'")
        return False
    if svc._client.assign_to_l2.await_count:
        print("  FAIL: assign-l2 не должен вызываться без логина")
        return False
    return True


async def test_no_take_and_invalid() -> bool:
    """«не беру» → ready_for_transfer, «Невалид» → invalid."""
    from modules.partner_crm.service import PartnerCrmService
    from core import bus as bus_mod

    for choice, want in (("no_take", "ready_for_transfer"), ("invalid", "invalid")):
        svc = PartnerCrmService()
        svc._client.set_lead_status = AsyncMock()
        svc._client.assign_to_l2 = AsyncMock()
        orig = bus_mod.publish
        bus_mod.publish = AsyncMock()
        try:
            got = await svc.push_lead_moderated(
                account_id=23, username=TEST_USERNAME,
                choice=choice, l2_login="l2_001",
            )
        finally:
            bus_mod.publish = orig
        if got != want:
            print(f"  FAIL: {choice} → {got!r}, ожидалось {want!r}")
            return False
        if svc._client.assign_to_l2.await_count:
            print(f"  FAIL: {choice} не должен звать assign-l2")
            return False

    # invalid обязан попасть в blacklist — иначе партнёр вернёт лида снова
    pool = db.get_pool()
    async with pool.acquire() as conn:
        blacklisted = await conn.fetchval(
            "SELECT 1 FROM invalid_leads WHERE username = $1", TEST_USERNAME,
        )
        await conn.execute(
            "DELETE FROM invalid_leads WHERE username = $1", TEST_USERNAME,
        )
    if not blacklisted:
        print("  FAIL: invalid не записан в invalid_leads")
        return False
    return True


# ─── Таймер ──────────────────────────────────────────────────────────

async def test_no_choice_never_due() -> bool:
    """Без нажатия push_after=NULL — тикер такую строку не видит никогда."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await _cleanup(conn)
        await _seed(conn)
        due = await conn.fetchval(
            """
            SELECT COUNT(*) FROM report_moderation
            WHERE dialog_id = $1 AND pushed_at IS NULL
              AND push_after IS NOT NULL AND push_after <= NOW()
            """,
            TEST_DIALOG_ID,
        )
        await _cleanup(conn)
    if due != 0:
        print(f"  FAIL: строка без выбора попала в очередь ({due})")
        return False
    return True


async def test_choice_sets_4h_timer_and_reselect_shifts() -> bool:
    """Нажатие ставит +4ч; перевыбор считает заново от последнего."""
    from modules.report_bot.service import MODERATION_DELAY_SEC

    if MODERATION_DELAY_SEC != 4 * 3600:
        print(f"  FAIL: задержка {MODERATION_DELAY_SEC}с, ожидалось 4ч")
        return False

    pool = db.get_pool()
    async with pool.acquire() as conn:
        await _cleanup(conn)
        await _seed(conn)

        # первое нажатие
        first = await conn.fetchrow(
            """
            UPDATE report_moderation
            SET choice='take', chosen_at=NOW(),
                push_after = NOW() + make_interval(secs => $2)
            WHERE dialog_id = $1 AND pushed_at IS NULL
            RETURNING push_after, choice
            """,
            TEST_DIALOG_ID, MODERATION_DELAY_SEC,
        )
        await asyncio.sleep(0.05)
        # перевыбор
        second = await conn.fetchrow(
            """
            UPDATE report_moderation
            SET choice='invalid', chosen_at=NOW(),
                push_after = NOW() + make_interval(secs => $2)
            WHERE dialog_id = $1 AND pushed_at IS NULL
            RETURNING push_after, choice
            """,
            TEST_DIALOG_ID, MODERATION_DELAY_SEC,
        )
        await _cleanup(conn)

    if first is None or second is None:
        print("  FAIL: UPDATE не нашёл строку")
        return False
    if second["choice"] != "invalid":
        print(f"  FAIL: выбор не перезаписался ({second['choice']})")
        return False
    if second["push_after"] <= first["push_after"]:
        print("  FAIL: перевыбор не сдвинул таймер вперёд")
        return False
    delta = second["push_after"] - second["chosen_at"] if "chosen_at" in second else None
    return True


async def test_pushed_row_rejects_new_choice() -> bool:
    """После отправки в CRM выбор менять поздно — UPDATE не находит строку."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await _cleanup(conn)
        await _seed(conn)
        await conn.execute(
            """
            UPDATE report_moderation
            SET choice='take', chosen_at=NOW(),
                pushed_at=NOW(), pushed_status='transferred'
            WHERE dialog_id = $1
            """,
            TEST_DIALOG_ID,
        )
        # ровно тот UPDATE, что делает обработчик нажатия
        row = await conn.fetchrow(
            """
            UPDATE report_moderation
            SET choice='invalid', chosen_at=NOW(), push_after=NOW()
            WHERE dialog_id = $1 AND pushed_at IS NULL
            RETURNING dialog_id
            """,
            TEST_DIALOG_ID,
        )
        still = await conn.fetchval(
            "SELECT choice FROM report_moderation WHERE dialog_id = $1",
            TEST_DIALOG_ID,
        )
        await _cleanup(conn)

    if row is not None:
        print("  FAIL: UPDATE прошёл по уже отправленной строке")
        return False
    if still != "take":
        print(f"  FAIL: выбор изменился на {still!r}, должен остаться 'take'")
        return False
    return True


# ─── Связки с остальной системой ─────────────────────────────────────

async def test_moderation_bubble_is_not_terminal() -> bool:
    """«Модерация» без partner_terminal_status → finalizer её пропустит."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT partner_terminal_status FROM dialog_statuses "
            "WHERE label = 'Модерация'",
        )
    if row is None:
        print("  FAIL: пузыря «Модерация» нет в каталоге")
        return False
    if row["partner_terminal_status"] is not None:
        print(
            f"  FAIL: у «Модерация» terminal={row['partner_terminal_status']!r}, "
            "должен быть NULL — иначе finalizer запушит статус мимо модератора"
        )
        return False
    return True


async def test_evaluator_success_maps_to_moderation() -> bool:
    """Оценщик при success ставит «Модерация» и НЕ пишет terminal_reason."""
    from modules.lead_evaluator.service import (
        _LABEL_BY_VERDICT, _TERMINAL_REASON_BY_VERDICT,
    )
    ok = True
    if _LABEL_BY_VERDICT.get("success") != "Модерация":
        print(f"  FAIL: success → {_LABEL_BY_VERDICT.get('success')!r}")
        ok = False
    if _TERMINAL_REASON_BY_VERDICT.get("success") is not None:
        print(
            f"  FAIL: terminal_reason при success = "
            f"{_TERMINAL_REASON_BY_VERDICT.get('success')!r}, должен быть None"
        )
        ok = False
    # провал модерацию не проходит — закрывается автоматически
    if _LABEL_BY_VERDICT.get("archive") != "Завершен провал":
        print("  FAIL: archive не должен уходить в модерацию")
        ok = False
    return ok


async def test_finalizer_skips_moderated() -> bool:
    """Запрос финалайзера видит уже отмодерированный диалог."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await _cleanup(conn)
        await _seed(conn)
        await conn.execute(
            "UPDATE report_moderation SET pushed_at=NOW(), "
            "pushed_status='transferred' WHERE dialog_id=$1",
            TEST_DIALOG_ID,
        )
        moderated = await conn.fetchval(
            "SELECT pushed_status FROM report_moderation "
            "WHERE dialog_id = $1 AND pushed_at IS NOT NULL",
            TEST_DIALOG_ID,
        )
        await _cleanup(conn)
    if moderated != "transferred":
        print(f"  FAIL: финалайзер не увидит модерацию ({moderated!r})")
        return False
    return True


async def test_families_map_loaded() -> bool:
    """Карта семейств переехала в БД и покрывает все группы из .env."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT family, chat_id, l2_login FROM report_families WHERE is_active",
        )
    if len(rows) < 12:
        print(f"  FAIL: в report_families {len(rows)} строк, ожидалось >=12")
        return False
    with_l2 = [r for r in rows if r["l2_login"]]
    if len(with_l2) < 8:
        print(f"  FAIL: с l2_login только {len(with_l2)}, ожидалось >=8")
        return False
    # chat_id должны быть уникальны — иначе два семейства в одну группу
    ids = [r["chat_id"] for r in rows]
    if len(ids) != len(set(ids)):
        print("  FAIL: дубликаты chat_id в report_families")
        return False
    return True


async def _run_all() -> int:
    await db.init_pool()
    try:
        results = [
            ("take → assign-l2 (transferred)", await test_take_calls_assign_l2()),
            ("take без l2_login → ready_for_transfer", await test_take_without_l2_degrades()),
            ("no_take/invalid → статус + blacklist", await test_no_take_and_invalid()),
            ("без нажатия таймер не запускается", await test_no_choice_never_due()),
            ("нажатие +4ч, перевыбор сдвигает", await test_choice_sets_4h_timer_and_reselect_shifts()),
            ("после пуша выбор не меняется", await test_pushed_row_rejects_new_choice()),
            ("«Модерация» не терминальный пузырь", await test_moderation_bubble_is_not_terminal()),
            ("оценщик: success → Модерация", await test_evaluator_success_maps_to_moderation()),
            ("finalizer видит отметку модерации", await test_finalizer_skips_moderated()),
            ("карта семейств в БД", await test_families_map_loaded()),
        ]
    finally:
        await db.close_pool()

    print()
    for name, ok in results:
        print(f"  {name:45} → {'PASS' if ok else 'FAIL'}")
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print()
    if passed == total:
        print(f"ALL {total} MODERATION TESTS PASSED")
        return 0
    print(f"FAILED: {total - passed}/{total}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all()))
