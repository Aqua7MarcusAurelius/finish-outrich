"""Диагностика прокси: доказать, что «плохие прокси» — на самом деле код.

Для каждого прокси делает ТРАНСПОРТНЫЙ SOCKS TCP-connect до:
  * production-DC Telegram (то, что реально нужно воркеру), и
  * нейтрального 1.1.1.1 (то, что РАНЬШЕ проверял probe).

Если Telegram доступен, а 1.1.1.1 — нет (или заметно хуже), значит «нестабильность
прокси» была ЛОЖНЫМ срабатыванием probe: он целил в 1.1.1.1, а серверный прокси
мог резать сторонние адреса. Сессии Telegram НЕ поднимаются — только транспорт,
никакого риска для аккаунтов.

Запуск (в контейнере app — там есть доступ к БД аккаунтов):
    docker compose exec -T app python -m scripts.proxy_diag
    docker compose exec -T app python -m scripts.proxy_diag --timeout 8
    # ad-hoc, без БД:
    docker compose exec -T app python -m scripts.proxy_diag \
        --proxy socks5://user:pass@host:port \
        --proxy 158.46.145.227:63795:user:pass
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys

from core.proxy import (
    NEUTRAL_TARGET,
    TELEGRAM_DC_TARGETS,
    check_socks5,
    mask,
)

_COLON_RX = re.compile(r"^([^:\s]+):(\d+):([^:\s]+):(.+)$")


def _to_url(value: str) -> str:
    """`host:port:user:pass` → `socks5://user:pass@host:port`; url оставляем как есть."""
    value = value.strip()
    if value.startswith("socks5://"):
        return value
    m = _COLON_RX.match(value)
    if m:
        host, port, user, pw = m.groups()
        return f"socks5://{user}:{pw}@{host}:{port}"
    return value


async def _load_proxies_from_db() -> list[tuple[str, str]]:
    """Читает прокси из accounts. Возвращает [(label, proxy_url), ...]."""
    from core import db as db_mod

    await db_mod.init_pool()
    try:
        pool = db_mod.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, proxy_primary, proxy_fallback FROM accounts "
                "WHERE COALESCE(proxy_primary, '') <> '' ORDER BY id"
            )
    finally:
        await db_mod.close_pool()

    out: list[tuple[str, str]] = []
    for r in rows:
        name = r["name"] or f"acc{r['id']}"
        if r["proxy_primary"]:
            out.append((f"{name}/{r['id']} primary", r["proxy_primary"]))
        if r["proxy_fallback"]:
            out.append((f"{name}/{r['id']} fallback", r["proxy_fallback"]))
    return out


async def _diag_one(label: str, url: str, timeout: float) -> dict:
    tg = await check_socks5(url, timeout=timeout, targets=TELEGRAM_DC_TARGETS)
    neutral = await check_socks5(url, timeout=timeout, targets=[NEUTRAL_TARGET])
    return {"label": label, "proxy": mask(url), "tg": tg, "neutral": neutral}


def _fmt(res: dict) -> str:
    if res.get("ok"):
        extra = res.get("target", "")
        return f"OK {res.get('latency_ms', '?')}ms {extra}".strip()
    return f"FAIL {res.get('error', '')}".strip()


async def _run(proxies: list[tuple[str, str]], timeout: float) -> int:
    if not proxies:
        print("Нет прокси для проверки (пустая БД или не переданы --proxy).")
        return 1

    results = await asyncio.gather(
        *(_diag_one(label, url, timeout) for label, url in proxies)
    )

    print()
    print(f"{'account/proxy':32} {'Telegram DC':28} {'1.1.1.1':28} verdict")
    print("-" * 100)
    false_negatives = 0
    both_dead = 0
    for r in results:
        tg_ok = r["tg"].get("ok")
        n_ok = r["neutral"].get("ok")
        if tg_ok and not n_ok:
            verdict = "ПРОКСИ ЖИВ — probe по 1.1.1.1 врал бы"
            false_negatives += 1
        elif tg_ok and n_ok:
            verdict = "живой"
        elif not tg_ok and n_ok:
            verdict = "TG недоступен, а сеть есть (маршрут/бан TG?)"
        else:
            verdict = "оба недоступны — прокси реально лёг"
            both_dead += 1
        print(f"{r['label']:32} {_fmt(r['tg']):28} {_fmt(r['neutral']):28} {verdict}")

    print()
    print(f"Итого: {len(results)} прокси; "
          f"живых по Telegram: {sum(1 for r in results if r['tg'].get('ok'))}; "
          f"ложных негативов probe (TG ok, 1.1.1.1 fail): {false_negatives}; "
          f"реально мёртвых: {both_dead}.")
    if false_negatives:
        print("⇒ Часть 'плохих прокси' — это ложные срабатывания probe по 1.1.1.1. "
              "После перевода probe на Telegram DC они перестанут считаться мёртвыми.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Диагностика прокси: Telegram DC vs 1.1.1.1")
    ap.add_argument("--proxy", action="append", default=[],
                    help="Прокси (socks5://... или host:port:user:pass). Можно несколько.")
    ap.add_argument("--timeout", type=float, default=8.0, help="Таймаут коннекта, сек.")
    args = ap.parse_args()

    if args.proxy:
        proxies = [(f"cli#{i+1}", _to_url(p)) for i, p in enumerate(args.proxy)]
    else:
        proxies = asyncio.run(_load_proxies_from_db())

    return asyncio.run(_run(proxies, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
