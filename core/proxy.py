"""
Работа с SOCKS5-прокси: парсинг URL, маскировка для логов, TCP-проверка.

Используется:
- modules/worker/wrapper.py  — подставляет прокси в Telethon
- modules/auth/service.py    — проверяет прокси перед запуском флоу
- api/routes/system.py       — endpoint /system/proxy-check
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import socks  # pysocks

# Живость прокси меряем по РЕАЛЬНОЙ достижимости Telegram, а НЕ нейтрального
# хоста. Серверный прокси может отлично ходить в Telegram, но резать/не пускать
# сторонние адреса (например 1.1.1.1) — и тогда проверка по нейтральному хосту
# ложно объявляла ХОРОШИЙ прокси мёртвым, из-за чего воркер зря переезжал на
# fallback и не возвращался (мнимая «нестабильность прокси»). Пробуем несколько
# production-DC Telegram на 443; прокси считается живым если достижим ХОТЯ БЫ
# ОДИН (одиночный DC может быть на обслуживании / гео-недоступен).
TELEGRAM_DC_TARGETS: list[tuple[str, int]] = [
    ("149.154.167.51", 443),   # DC2 (Amsterdam) — основной для большинства
    ("149.154.175.53", 443),   # DC1 (Miami)     — запасная цель
]
# Нейтральный адрес — оставлен для ДИАГНОСТИКИ (сравнить TG vs произвольный хост).
NEUTRAL_TARGET: tuple[str, int] = ("1.1.1.1", 443)
DEFAULT_TIMEOUT = 5.0


def parse_socks5(url: str) -> tuple:
    """
    `socks5://user:pass@host:port` → кортеж для параметра `proxy` Telethon:
    (socks.SOCKS5, host, port, rdns=True, user, pass)
    """
    parsed = urlparse(url)
    if parsed.scheme != "socks5":
        raise ValueError(f"Поддерживается только socks5, получено: {parsed.scheme!r}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"Некорректный прокси: {url}")
    return (
        socks.SOCKS5,
        parsed.hostname,
        parsed.port,
        True,  # rdns — резолвим DNS на стороне прокси
        parsed.username or None,
        parsed.password or None,
    )


def mask(url: str | None) -> str:
    """Замазать user:pass в прокси-строке. Для логов и публикаций в шину."""
    if not url:
        return ""
    try:
        p = urlparse(url)
    except Exception:
        return "***"
    if p.username or p.password:
        return f"{p.scheme}://***@{p.hostname}:{p.port}"
    return url


def _connect_once(
    url: str, target: tuple[str, int], timeout: float,
) -> tuple[bool, int | None, str | None]:
    """Один TCP-connect через SOCKS до target=(host, port). pysocks блокирующий."""
    try:
        _, host, port, _rdns, user, password = parse_socks5(url)
    except Exception as e:
        return False, None, f"invalid proxy url: {e}"

    s = socks.socksocket()
    s.set_proxy(socks.SOCKS5, host, port,
                rdns=True, username=user, password=password)
    s.settimeout(timeout)
    started = time.monotonic()
    try:
        s.connect(target)
        latency = int((time.monotonic() - started) * 1000)
        return True, latency, None
    except Exception as e:
        err = str(e) or type(e).__name__
        return False, None, err
    finally:
        try:
            s.close()
        except Exception:
            pass


def _probe_sync(
    url: str, timeout: float, targets: list[tuple[str, int]],
) -> tuple[bool, int | None, str | None, tuple[str, int] | None]:
    """Синхронная часть — в to_thread. Пробуем список целей; ok если достижима
    ХОТЯ БЫ ОДНА (например любой TG DC). Возвращает (ok, latency, err, hit)."""
    last_err: str | None = None
    for target in targets:
        ok, latency, err = _connect_once(url, target, timeout)
        if ok:
            return True, latency, None, target
        last_err = err
    return False, None, last_err, None


async def check_socks5(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    targets: list[tuple[str, int]] | None = None,
) -> dict:
    """
    Проверка живости прокси по РЕАЛЬНОЙ достижимости Telegram (по умолчанию —
    production-DC). Транспортная (SOCKS TCP-connect), сессию Telegram НЕ поднимает.

    Возвращает:
        {"proxy": masked, "ok": True,  "latency_ms": 142, "target": "149.154.167.51:443"}
        {"proxy": masked, "ok": False, "error": "connection refused"}

    `targets` — список (host, port); по умолчанию TELEGRAM_DC_TARGETS. Для
    диагностики можно передать [NEUTRAL_TARGET] и сравнить TG vs произвольный хост.
    """
    tgts = targets if targets is not None else TELEGRAM_DC_TARGETS
    ok, latency, error, hit = await asyncio.to_thread(_probe_sync, url, timeout, tgts)
    result: dict = {"proxy": mask(url), "ok": ok}
    if ok:
        result["latency_ms"] = latency
        if hit is not None:
            result["target"] = f"{hit[0]}:{hit[1]}"
    else:
        result["error"] = error
    return result