"""Smoke-тест Fix #24: непрерывный индикатор «печатает» через все сегменты.

Собеседник должен видеть одно сплошное «печатает» от начала ответа и до
последнего сообщения — как будто человек набирает длинную мысль и шлёт
её частями. Telegram гасит статус через ~6с, поэтому его надо обновлять.

Проверяем:
  * cancel_typing зовётся ровно один раз — после последнего сегмента;
  * во время долгих пауз typing обновляется (не мигает);
  * порядок вызовов корректный.

Запуск:
    docker compose exec -T app python -m tests.smoke.test_typing_continuity
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, patch


class FakeWrapper:
    """Двойник TelegramWrapper с журналом вызовов."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._msg_id = 100

    async def set_typing(self, user_id: int) -> None:
        self.calls.append("set_typing")

    async def cancel_typing(self, user_id: int) -> None:
        self.calls.append("cancel_typing")

    async def send_message(self, user_id: int, text: str) -> dict:
        self.calls.append(f"send:{text[:20]}")
        self._msg_id += 1
        return {"telegram_message_id": self._msg_id, "date": None}


def _make_session(wrapper: FakeWrapper):
    """Собрать AutoChatSession с подменённым wrapper'ом."""
    from modules.autochat.session import AutoChatSession

    row = {
        "id": 999999, "account_id": 24, "dialog_id": 555,
        "telegram_user_id": 777, "target_username": "test",
        "system_prompt": "", "initial_prompt": "", "initial_sent_text": "",
        "status": "active", "in_chat": True,
        "last_our_activity_at": None, "last_their_message_at": None,
    }
    return AutoChatSession(
        row=row,
        get_wrapper=lambda acc: wrapper,
        on_finished_by_llm=None,
    )


async def test_cancel_typing_only_after_last_segment() -> bool:
    """3 сегмента → cancel_typing ровно 1 раз, в самом конце."""
    from modules.autochat import session as sess_mod

    wrapper = FakeWrapper()
    s = _make_session(wrapper)

    # Ускоряем: настройки минимальные, refresh не срабатывает
    async def fake_setting(key: str) -> int:
        return {
            "autochat.typing_ms_per_char": 1,
            "autochat.typing_min_ms": 10,
            "autochat.typing_max_ms": 20,
            "autochat.segment_gap_min_ms": 10,
            "autochat.segment_gap_max_ms": 20,
        }.get(key, 0)

    with patch.object(sess_mod, "_get_setting_int", fake_setting), \
         patch.object(s, "_persist_state", AsyncMock()), \
         patch.object(sess_mod.bus, "publish", AsyncMock()):
        for i in (1, 2, 3):
            await s._send_segment({
                "text": f"сегмент {i}", "index": i, "total": 3,
                "parent_id": None,
            })

    cancels = wrapper.calls.count("cancel_typing")
    if cancels != 1:
        print(f"  FAIL: cancel_typing вызван {cancels} раз, ожидался 1")
        print(f"  calls: {wrapper.calls}")
        return False

    # cancel должен идти ПЕРЕД последней отправкой (гасим и шлём финал)
    last_cancel = len(wrapper.calls) - 1 - wrapper.calls[::-1].index("cancel_typing")
    last_send = len(wrapper.calls) - 1 - next(
        i for i, c in enumerate(wrapper.calls[::-1]) if c.startswith("send:")
    )
    if last_cancel > last_send:
        print(f"  FAIL: cancel после последней отправки — calls: {wrapper.calls}")
        return False
    return True


async def test_single_segment_still_cancels() -> bool:
    """Одиночный сегмент (total=1) — cancel_typing тоже должен быть."""
    from modules.autochat import session as sess_mod

    wrapper = FakeWrapper()
    s = _make_session(wrapper)

    async def fake_setting(key: str) -> int:
        return {
            "autochat.typing_ms_per_char": 1,
            "autochat.typing_min_ms": 10,
            "autochat.typing_max_ms": 20,
        }.get(key, 0)

    with patch.object(sess_mod, "_get_setting_int", fake_setting), \
         patch.object(s, "_persist_state", AsyncMock()), \
         patch.object(sess_mod.bus, "publish", AsyncMock()):
        await s._send_segment({
            "text": "один сегмент", "index": 1, "total": 1, "parent_id": None,
        })

    ok = wrapper.calls.count("cancel_typing") == 1
    if not ok:
        print(f"  FAIL: calls = {wrapper.calls}")
    return ok


async def test_typing_refreshed_during_long_sleep() -> bool:
    """Пауза длиннее _TYPING_REFRESH_SEC → typing обновляется."""
    from modules.autochat import session as sess_mod

    wrapper = FakeWrapper()
    s = _make_session(wrapper)

    # refresh каждые 0.05с, спим 0.26с → ожидаем ~4-5 обновлений
    with patch.object(sess_mod, "_TYPING_REFRESH_SEC", 0.05):
        await s._sleep_keeping_typing(wrapper, 0.26)

    refreshes = wrapper.calls.count("set_typing")
    if refreshes < 3:
        print(f"  FAIL: typing обновлён {refreshes} раз за 0.26с "
              f"при refresh=0.05с, ожидалось >=3")
        return False
    return True


async def test_short_sleep_no_extra_refresh() -> bool:
    """Пауза короче интервала обновления → лишних set_typing нет."""
    from modules.autochat import session as sess_mod

    wrapper = FakeWrapper()
    s = _make_session(wrapper)

    with patch.object(sess_mod, "_TYPING_REFRESH_SEC", 5.0):
        await s._sleep_keeping_typing(wrapper, 0.05)

    refreshes = wrapper.calls.count("set_typing")
    ok = refreshes == 0
    if not ok:
        print(f"  FAIL: ожидалось 0 обновлений, получено {refreshes}")
    return ok


async def _run_all() -> int:
    results = [
        ("cancel_typing только после последнего сегмента",
         await test_cancel_typing_only_after_last_segment()),
        ("одиночный сегмент тоже гасит typing",
         await test_single_segment_still_cancels()),
        ("typing обновляется в долгой паузе",
         await test_typing_refreshed_during_long_sleep()),
        ("короткая пауза без лишних обновлений",
         await test_short_sleep_no_extra_refresh()),
    ]
    print()
    for name, ok in results:
        print(f"  {name:48} → {'PASS' if ok else 'FAIL'}")
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print()
    if passed == total:
        print(f"ALL {total} TYPING-CONTINUITY TESTS PASSED")
        return 0
    print(f"FAILED: {total - passed}/{total}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all()))
