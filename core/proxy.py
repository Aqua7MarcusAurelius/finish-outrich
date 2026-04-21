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

# Нейтральный TCP-адрес для проверки работоспособности прокси.
# Cloudflare DNS — стабильно доступен и принимает TLS на 443.
TEST_HOST = "1.1.1.1"
TEST_PORT = 443
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


def _probe_sync(url: str, timeout: float) -> tuple[bool, int | None, str | None]:
    """Синхронная часть — выполняется в to_thread, pysocks блокирующий."""
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
        s.connect((TEST_HOST, TEST_PORT))
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


async def check_socks5(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """
    Асинхронная обёртка. Возвращает:
        {"proxy": masked, "ok": True,  "latency_ms": 142}
        {"proxy": masked, "ok": False, "error": "connection refused"}
    """
    ok, latency, error = await asyncio.to_thread(_probe_sync, url, timeout)
    result: dict = {"proxy": mask(url), "ok": ok}
    if ok:
        result["latency_ms"] = latency
    else:
        result["error"] = error
    return result