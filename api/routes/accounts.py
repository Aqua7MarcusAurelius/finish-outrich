"""
GET /accounts — список аккаунтов для страницы «Диалоги».

По контракту (docs/ui/web_ui_api_contract_v1.md): `worker.list` с добавленными
`dialogs_count` (из БД) и `last_event_at` (из events_archive).

Роут живёт здесь, а не в worker_manager, потому что это чисто read-модель
для UI — она агрегирует данные из worker_manager, истории и шины.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request

from core import db

router = APIRouter(tags=["accounts"])


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


@router.get("/accounts")
async def list_accounts(request: Request) -> list[dict[str, Any]]:
    manager = request.app.state.worker_manager
    workers = await manager.list_workers()

    if not workers:
        return []

    ids = [w["account_id"] for w in workers]

    pool = db.get_pool()
    async with pool.acquire() as conn:
        dialog_counts = {
            r["account_id"]: r["dialogs_count"]
            for r in await conn.fetch(
                """
                SELECT account_id, COUNT(*)::int AS dialogs_count
                FROM dialogs
                WHERE account_id = ANY($1::int[])
                GROUP BY account_id
                """,
                ids,
            )
        }
        # Последнее событие по аккаунту — из архива шины.
        # Для read-панели этого достаточно: live-события UI подхватит через SSE.
        last_event_times = {
            r["account_id"]: r["last_time"]
            for r in await conn.fetch(
                """
                SELECT account_id, MAX(time) AS last_time
                FROM events_archive
                WHERE account_id = ANY($1::int[])
                GROUP BY account_id
                """,
                ids,
            )
        }

    out: list[dict[str, Any]] = []
    for w in workers:
        acc_id = w["account_id"]
        out.append({
            "id": acc_id,
            "name": w["name"],
            "phone": w["phone"],
            "status": w["status"],
            "is_active": w["is_active"],
            "dialogs_count": dialog_counts.get(acc_id, 0),
            "last_event_at": _iso(last_event_times.get(acc_id)),
        })
    return out
