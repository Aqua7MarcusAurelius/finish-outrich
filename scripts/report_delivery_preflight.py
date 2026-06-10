"""
Пре-флайт доставки отчётов — БЕЗ отправки сообщений.

Подключает сервис-аккаунт (имя из SERVICE_ACCOUNT_NAME) его сессией из БД,
читает список диалогов и проверяет, что КАЖДЫЙ chat_id из REPORT_GROUP_MAP
реально присутствует среди групп аккаунта (по точному marked-id, с
названием группы). Ничего никуда не шлёт, статусов не меняет.

Карту групп берём из окружения (`REPORT_GROUP_MAP`), чтобы ничего не
хардкодить — удобно гонять через:
    docker exec -e REPORT_GROUP_MAP='{"SAD": -5240340499, ...}' \
        tgf_app python scripts/report_delivery_preflight.py

Код выхода: 0 — все резолвятся; 2 — часть не найдена; 1 — не удалось
подключиться / нет аккаунта / пустая карта.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db                                              # noqa: E402
from core.config import settings                                 # noqa: E402
from modules.worker.wrapper import (                             # noqa: E402
    ProxyUnavailable,
    SessionExpired,
    TelegramWrapper,
)


async def main() -> int:
    gmap = settings.report_group_map
    if not gmap:
        print("REPORT_GROUP_MAP пуст — нечего проверять.")
        return 1

    await db.init_pool()
    try:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, session_data, proxy_primary, proxy_fallback "
                "FROM accounts WHERE name = $1 ORDER BY id LIMIT 1",
                settings.SERVICE_ACCOUNT_NAME,
            )
        if row is None:
            print(f"сервис-аккаунт name='{settings.SERVICE_ACCOUNT_NAME}' не найден.")
            return 1
        if not row["session_data"]:
            print(f"сервис-аккаунт '{settings.SERVICE_ACCOUNT_NAME}' не авторизован "
                  f"(нет session_data).")
            return 1

        wrapper = TelegramWrapper(
            account_id=int(row["id"]),
            api_id=settings.TELEGRAM_API_ID,
            api_hash=settings.TELEGRAM_API_HASH,
            session_data=row["session_data"],
            proxy_primary=row["proxy_primary"],
            proxy_fallback=row["proxy_fallback"],
        )
        try:
            await wrapper.connect(require_authorized=True)
        except (SessionExpired, ProxyUnavailable) as e:
            print(f"подключение не удалось: {e}")
            return 1

        try:
            # Собираем ВСЕ групповые диалоги аккаунта: marked-id → название.
            # Это чистое чтение, ничего не отправляем и не помечаем прочитанным.
            present: dict[int, str] = {}
            async for d in wrapper.client.iter_dialogs():
                if getattr(d, "is_group", False):
                    present[int(d.id)] = d.name or "(без названия)"

            print(f"сервис-аккаунт '{row['name']}' подключён; "
                  f"групп в списке: {len(present)}\n")

            ok = 0
            for family, chat_id in gmap.items():
                title = present.get(int(chat_id))
                if title is not None:
                    print(f"  OK    {family:<10} {chat_id}  →  «{title}»")
                    ok += 1
                else:
                    print(f"  FAIL  {family:<10} {chat_id}  →  НЕ найдено среди групп аккаунта")

            print(f"\n=== резолвится {ok}/{len(gmap)} групп ===")
            return 0 if ok == len(gmap) else 2
        finally:
            await wrapper.disconnect()
    finally:
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
