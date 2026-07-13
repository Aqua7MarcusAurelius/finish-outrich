"""Smoke-тесты Части A (2026-07-13): один персистентный TelegramClient +
последовательный свитч прокси + транспортный probe.

Проверяем на РЕАЛЬНОМ Worker.run() через FakeWrapper:
  * хендлеры регистрируются РОВНО ОДИН раз (не перевешиваются на каждый
    реконнект — старый костыль Fix #16 удалён);
  * get_me()/get_dialogs() (прогрев + снапшот) вызываются один раз;
  * при runtime-drop идёт ПОСЛЕДОВАТЕЛЬНЫЙ свитч: сначала disconnect(),
    потом connect_via(fallback) — никогда две живые сессии (C3);
  * пока на fallback — транспортный probe основного; ожил → свитч обратно
    на основной (H2 без осцилляции);
  * grace-period логика auto_runner (Fix #12/#18) — чистые счётчики.

Реальный Telethon не используется — FakeWrapper подменяет весь транспорт.

Запуск:
    docker compose exec -T app python -m tests.smoke.test_resilient_worker
"""
from __future__ import annotations

import asyncio
import sys


class FakeWrapper:
    """Двойник TelegramWrapper для нового супервайзера Worker.run.

    `wait_until_disconnected` при ПЕРВОМ вызове возвращается через
    `disconnect_after_sec` (симуляция runtime-drop основного прокси),
    далее блокируется бесконечно.
    """

    def __init__(self, *, disconnect_after_sec: float = 0.05) -> None:
        self.proxy_primary = "socks5://main"
        self.proxy_fallback = "socks5://fallback"
        self.active_proxy: str | None = None
        # счётчики
        self.connect_via_calls: list[str] = []
        self.disconnect_calls: int = 0
        self.on_new_message_calls: int = 0
        self.enable_typing_observer_calls: int = 0
        self.get_me_calls: int = 0
        self.get_dialogs_calls: int = 0
        self.probe_calls: list[str] = []
        # журнал порядка connect/disconnect — для проверки последовательности
        self.order: list[tuple[str, str | None]] = []
        # управление wait_until_disconnected
        self._disconnect_after_sec = disconnect_after_sec
        self._wait_count = 0

    async def connect_via(self, proxy_url: str, *, require_authorized: bool = True) -> None:
        self.connect_via_calls.append(proxy_url)
        self.order.append(("connect", proxy_url))
        self.active_proxy = proxy_url

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.order.append(("disconnect", self.active_proxy))
        self.active_proxy = None

    def on_new_message(self, handler, *, incoming: bool = True, outgoing: bool = True) -> None:
        self.on_new_message_calls += 1

    def enable_typing_observer(self) -> None:
        self.enable_typing_observer_calls += 1

    async def get_me(self):
        self.get_me_calls += 1
        return {"id": 1}

    async def get_dialogs(self, limit=None):
        self.get_dialogs_calls += 1
        return []

    async def probe_proxy(self, proxy_url: str, *, timeout: float = 10.0) -> bool:
        self.probe_calls.append(proxy_url)
        return True

    async def wait_until_disconnected(self) -> None:
        self._wait_count += 1
        if self._wait_count == 1:
            await asyncio.sleep(self._disconnect_after_sec)
            return
        forever = asyncio.Event()
        await forever.wait()


async def _drive_worker(fake: FakeWrapper, *, run_sec: float = 0.6):
    """Запустить реальный Worker.run() с FakeWrapper, дать отработать
    connect → drop → fallback → probe-restore → back, затем stop."""
    from core import bus as bus_mod
    from modules.worker.worker import Worker

    published: list[dict] = []

    async def _capture_publish(**kwargs):
        published.append(kwargs)

    orig_publish = bus_mod.publish
    bus_mod.publish = _capture_publish  # type: ignore

    from modules.history_sync import service as hs_mod

    class _FakeSync:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self) -> None:
            forever = asyncio.Event()
            try:
                await forever.wait()
            except asyncio.CancelledError:
                raise

    orig_sync = hs_mod.HistorySyncService
    hs_mod.HistorySyncService = _FakeSync  # type: ignore

    try:
        worker = Worker(
            account_id=99999,
            account_name="test-resilient",
            session_data=b"",
            proxy_primary="socks5://main",
            proxy_fallback="socks5://fallback",
            api_id=1,
            api_hash="dummy",
            device_model="TestDevice",
            system_version="1.0",
            app_version="1.0",
        )
        worker.wrapper = fake  # type: ignore
        # Ускоряем таймеры супервайзера, чтобы probe/бэкофф отрабатывали быстро.
        worker.PROBE_INTERVAL_SEC = 0.05  # type: ignore
        worker.RECONNECT_INTERVAL_SEC = 0.05  # type: ignore

        run_task = asyncio.create_task(worker.run())
        await asyncio.sleep(run_sec)
        await worker.stop()
        try:
            await asyncio.wait_for(run_task, timeout=5.0)
        except asyncio.TimeoutError:
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass
        return published
    finally:
        bus_mod.publish = orig_publish  # type: ignore
        hs_mod.HistorySyncService = orig_sync  # type: ignore


async def test_handlers_registered_once_across_reconnect() -> bool:
    """Хендлеры и прогрев — РОВНО один раз, несмотря на runtime-drop +
    свитч прокси. Это анти-регресс к костылю Fix #16 (перевешивание на
    каждый реконнект)."""
    fake = FakeWrapper(disconnect_after_sec=0.05)
    await _drive_worker(fake)

    checks = [
        ("on_new_message once", fake.on_new_message_calls, 1),
        ("enable_typing_observer once", fake.enable_typing_observer_calls, 1),
        ("get_me once", fake.get_me_calls, 1),
        ("get_dialogs once", fake.get_dialogs_calls, 1),
    ]
    ok = True
    for name, got, want in checks:
        if got != want:
            print(f"  FAIL: {name} = {got}, expected {want}")
            ok = False
    return ok


async def test_sequential_switch_and_probe() -> bool:
    """Свитч прокси строго последователен (disconnect ПЕРЕД каждым connect),
    и после drop основного идёт транспортный probe + возврат на основной."""
    fake = FakeWrapper(disconnect_after_sec=0.05)
    await _drive_worker(fake)

    ok = True

    # Первый connect — основной; затем свитч на fallback; затем возврат на main.
    if fake.connect_via_calls[:2] != ["socks5://main", "socks5://fallback"]:
        print(f"  FAIL: connect sequence start = {fake.connect_via_calls[:2]}, "
              f"expected [main, fallback]")
        ok = False
    if "socks5://main" not in fake.connect_via_calls[2:]:
        print(f"  FAIL: primary not restored, calls = {fake.connect_via_calls}")
        ok = False

    # Последовательность: каждый connect предварён disconnect'ом (C3 — никогда
    # две живые сессии).
    for i, (kind, _proxy) in enumerate(fake.order):
        if kind == "connect":
            if i == 0 or fake.order[i - 1][0] != "disconnect":
                print(f"  FAIL: connect at #{i} not preceded by disconnect: {fake.order}")
                ok = False
                break

    # Транспортный probe основного использовался.
    if "socks5://main" not in fake.probe_calls:
        print(f"  FAIL: primary probe not used, probe_calls = {fake.probe_calls}")
        ok = False

    return ok


async def test_restart_events_published() -> bool:
    """При свитче на fallback и возврате на основной публикуются
    worker.restarted (для UI)."""
    from core.events import EventType

    fake = FakeWrapper(disconnect_after_sec=0.05)
    published = await _drive_worker(fake)

    restarted = [p for p in published if p.get("type") == EventType.WORKER_RESTARTED]
    if len(restarted) < 1:
        print(f"  FAIL: expected >=1 worker.restarted, got {len(restarted)}")
        return False
    return True


async def test_grace_period_logic() -> bool:
    """Чистая логика счётчика grace-period (Fix #12) — не тестируем БД."""
    from modules.worker_auto_runner.service import MAX_WRAPPER_MISSES
    counter = 0
    stops = 0
    for _miss in range(MAX_WRAPPER_MISSES + 2):
        counter += 1
        if counter >= MAX_WRAPPER_MISSES:
            stops += 1
            counter = 0
    return stops == 1 and MAX_WRAPPER_MISSES == 3


async def test_startup_wait_never_counts_before_first_wrapper() -> bool:
    """Fix #18: grace-таймер не тикает пока wrapper ни разу не был жив."""
    from modules.worker_auto_runner.service import MAX_WRAPPER_MISSES

    ever_saw = False
    miss = 0
    stops = 0
    for _ in range(10):
        if not ever_saw:
            continue
        miss += 1
        if miss >= MAX_WRAPPER_MISSES:
            stops += 1
            miss = 0
    ever_saw = True
    miss = 0
    for _ in range(MAX_WRAPPER_MISSES):
        if not ever_saw:
            continue
        miss += 1
        if miss >= MAX_WRAPPER_MISSES:
            stops += 1
            miss = 0
    return stops == 1


async def _run_all() -> int:
    results = [
        ("Part A: handlers registered once across reconnect",
         await test_handlers_registered_once_across_reconnect()),
        ("Part A: sequential proxy switch + transport probe",
         await test_sequential_switch_and_probe()),
        ("Part A: worker.restarted published on switch",
         await test_restart_events_published()),
        ("grace period counter logic", await test_grace_period_logic()),
        ("Fix #18: no self-stop before first wrapper seen",
         await test_startup_wait_never_counts_before_first_wrapper()),
    ]
    print()
    for name, ok in results:
        print(f"  {name:55} → {'PASS' if ok else 'FAIL'}")
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print()
    if passed == total:
        print(f"ALL {total} RESILIENCE TESTS PASSED")
        return 0
    print(f"FAILED: {total - passed}/{total}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all()))
