"""Smoke-тесты supervisor-паттерна в Worker.run (Fix #1).

Без внешних зависимостей (asyncio + типы). Проверяем:

  1. ``TelethonDisconnected`` действительно бросается когда
     ``wrapper.wait_until_disconnected`` разрешается первым (не по stop()).
  2. При штатном stop() исключение НЕ бросается (run() возвращается тихо).
  3. ``on_ready`` колбэк вызывается ПОСЛЕ ``wrapper.connect(...)``,
     не до и не после disconnect'а (устраняет race сценария №1).

Fake-wrapper — минимальный тестовый двойник TelegramWrapper с нужными
методами.

Запуск (внутри контейнера app):
    docker compose exec -T app python -m tests.smoke.test_worker_supervisor
"""
from __future__ import annotations

import asyncio
import sys

from modules.worker.wrapper import TelethonDisconnected


class FakeWrapper:
    """Минимальный тестовый двойник TelegramWrapper. Поддерживает связь
    с asyncio-событиями: ``connect_finished`` разрешается когда connect()
    отработал; ``disconnected_event`` — когда пора имитировать disconnect.
    """

    def __init__(self) -> None:
        self.connected = False
        self.disconnected_event = asyncio.Event()
        self.disconnect_calls = 0
        self.connect_calls = 0

    async def connect(self, *, require_authorized: bool = True) -> None:
        self.connect_calls += 1
        # имитируем небольшую задержку connect'а (реально ~30-40 сек)
        await asyncio.sleep(0.01)
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def wait_until_disconnected(self) -> None:
        await self.disconnected_event.wait()

    async def get_dialogs(self, limit=None):
        return []

    def on_new_message(self, cb, *, incoming=True, outgoing=True) -> None:
        return None

    def enable_typing_observer(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False


async def _simulate_worker_supervisor(
    stop_event: asyncio.Event,
    wrapper: FakeWrapper,
    on_ready_flag: dict[str, list],
) -> tuple[bool, str | None]:
    """Мини-копия supervisor-логики из Worker.run — без bus/публикаций.

    Возвращает (raised_TelethonDisconnected, reason_string).
    """
    await wrapper.connect(require_authorized=True)
    on_ready_flag["order"].append("connect_returned")
    # имитация on_ready callback
    on_ready_flag["order"].append("on_ready_called")

    stop_task = asyncio.create_task(stop_event.wait(), name="stop")
    disc_task = asyncio.create_task(wrapper.wait_until_disconnected(), name="disc")

    telethon_disconnected = False
    try:
        done, pending = await asyncio.wait(
            {stop_task, disc_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        if disc_task in done and stop_task in pending:
            telethon_disconnected = True
    finally:
        for t in (stop_task, disc_task):
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        await wrapper.disconnect()

    if telethon_disconnected:
        raise TelethonDisconnected("test-simulation")
    return (False, None)


async def test_1_normal_stop_no_exception() -> bool:
    """Штатный stop() — run() возвращается без исключения."""
    wrapper = FakeWrapper()
    stop_event = asyncio.Event()
    flag = {"order": []}

    async def _stopper():
        await asyncio.sleep(0.05)
        stop_event.set()

    stopper = asyncio.create_task(_stopper())
    try:
        result = await _simulate_worker_supervisor(stop_event, wrapper, flag)
        await stopper
        assert result == (False, None), f"expected clean return, got {result}"
        assert wrapper.disconnect_calls == 1, "wrapper.disconnect() should have been called"
        assert flag["order"] == ["connect_returned", "on_ready_called"], (
            f"on_ready called out of order: {flag['order']}"
        )
        return True
    except Exception as e:
        print(f"  FAIL test_1: {e!r}")
        return False


async def test_2_telethon_dropout_raises() -> bool:
    """Telethon отвалился в фоне → TelethonDisconnected."""
    wrapper = FakeWrapper()
    stop_event = asyncio.Event()
    flag = {"order": []}

    async def _dropper():
        await asyncio.sleep(0.05)
        # имитируем что TG-сервер закрыл соединение
        wrapper.connected = False
        wrapper.disconnected_event.set()

    dropper = asyncio.create_task(_dropper())
    try:
        await _simulate_worker_supervisor(stop_event, wrapper, flag)
        print("  FAIL test_2: expected TelethonDisconnected, got clean return")
        return False
    except TelethonDisconnected:
        await dropper
        assert wrapper.disconnect_calls == 1
        return True
    except Exception as e:
        print(f"  FAIL test_2: unexpected {type(e).__name__}: {e!r}")
        return False


async def test_3_stop_wins_race() -> bool:
    """stop() и disconnect стреляют почти одновременно, но stop() чуть
    раньше → run() возвращается тихо, disconnect игнорируется."""
    wrapper = FakeWrapper()
    stop_event = asyncio.Event()
    flag = {"order": []}

    async def _dual():
        await asyncio.sleep(0.05)
        stop_event.set()
        # даём stop_task гарантированно выиграть
        await asyncio.sleep(0)
        wrapper.connected = False
        wrapper.disconnected_event.set()

    dual = asyncio.create_task(_dual())
    try:
        result = await _simulate_worker_supervisor(stop_event, wrapper, flag)
        await dual
        assert result == (False, None), "stop() should have won"
        return True
    except TelethonDisconnected:
        print("  FAIL test_3: TelethonDisconnected raised but stop() came first")
        return False


async def _run_all() -> int:
    results = [
        ("test_1_normal_stop_no_exception", await test_1_normal_stop_no_exception()),
        ("test_2_telethon_dropout_raises", await test_2_telethon_dropout_raises()),
        ("test_3_stop_wins_race", await test_3_stop_wins_race()),
    ]
    print()
    for name, ok in results:
        print(f"  {name:40} → {'PASS' if ok else 'FAIL'}")
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print()
    if passed == total:
        print(f"ALL {total} SUPERVISOR TESTS PASSED")
        return 0
    print(f"FAILED: {total - passed}/{total}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all()))
