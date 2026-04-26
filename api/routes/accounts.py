"""
GET /accounts — список аккаунтов для страницы «Диалоги».

По контракту (docs/ui/web_ui_api_contract_v1.md): `worker.list` с добавленными
`dialogs_count` (из БД) и `last_event_at` (из events_archive).

Роут живёт здесь, а не в worker_manager, потому что это чисто read-модель
для UI — она агрегирует данные из worker_manager, истории и шины.

Также тут CRUD для per-worker промтов (account_prompts) — редактор в UI
вызывает GET/PUT /accounts/{id}/prompts. Промты используются модулем
autochat: пустой reply_system блокирует автоответы (см. session.py),
пустой initial_system блокирует POST /autochat/start.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import db
from modules.autochat.prompts import DEFAULT_FORBIDDEN, DEFAULT_FORMAT_REPLY

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


# ─────────────────────────────────────────────────────────────────────
# Per-worker промты для AutoChat
# ─────────────────────────────────────────────────────────────────────

class PromptsIn(BaseModel):
    fabula: str = Field(default="", max_length=20000)
    bio: str = Field(default="", max_length=20000)
    style: str = Field(default="", max_length=20000)
    forbidden: str = Field(default="", max_length=20000)
    length_hint: str = Field(default="", max_length=5000)
    goals: str = Field(default="", max_length=20000)
    format_reply: str = Field(default="", max_length=20000)
    examples: str = Field(default="", max_length=20000)
    initial_system: str = Field(default="", max_length=20000)


def _account_not_found() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": {"code": "ACCOUNT_NOT_FOUND", "message": "Аккаунт не найден"}},
    )


def _row_to_prompts_dict(account_id: int, row) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "fabula": row["fabula"],
        "bio": row["bio"],
        "style": row["style"],
        "forbidden": row["forbidden"],
        "length_hint": row["length_hint"],
        "goals": row["goals"],
        "format_reply": row["format_reply"],
        "examples": row["examples"],
        "initial_system": row["initial_system"],
        "updated_at": _iso(row["updated_at"]),
    }


@router.get("/accounts/{account_id}/prompts")
async def get_account_prompts(account_id: int):
    """
    Возвращает per-worker промт-конфиг.

    Если строки в `account_prompts` ещё нет — отдаём дефолт-overlay для
    `forbidden` и `format_reply` (чтобы оператор не забыл про <msg>-теги
    и базовые запреты при первом редактировании). Остальные поля пустые.
    Этот overlay применяется ТОЛЬКО при отсутствии строки; после первого
    PUT возвращаем то что в БД, без подмесов.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        acc = await conn.fetchval("SELECT id FROM accounts WHERE id = $1", account_id)
        if acc is None:
            return _account_not_found()
        row = await conn.fetchrow(
            """
            SELECT fabula, bio, style, forbidden, length_hint, goals,
                   format_reply, examples, initial_system, updated_at
            FROM account_prompts WHERE account_id = $1
            """,
            account_id,
        )
    if row is None:
        return {
            "account_id": account_id,
            "fabula": "",
            "bio": "",
            "style": "",
            "forbidden": DEFAULT_FORBIDDEN,
            "length_hint": "",
            "goals": "",
            "format_reply": DEFAULT_FORMAT_REPLY,
            "examples": "",
            "initial_system": "",
            "updated_at": None,
        }
    return _row_to_prompts_dict(account_id, row)


@router.put("/accounts/{account_id}/prompts")
async def put_account_prompts(account_id: int, payload: PromptsIn):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        acc = await conn.fetchval("SELECT id FROM accounts WHERE id = $1", account_id)
        if acc is None:
            return _account_not_found()
        row = await conn.fetchrow(
            """
            INSERT INTO account_prompts (
                account_id, fabula, bio, style, forbidden, length_hint,
                goals, format_reply, examples, initial_system, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
            ON CONFLICT (account_id) DO UPDATE SET
                fabula = EXCLUDED.fabula,
                bio = EXCLUDED.bio,
                style = EXCLUDED.style,
                forbidden = EXCLUDED.forbidden,
                length_hint = EXCLUDED.length_hint,
                goals = EXCLUDED.goals,
                format_reply = EXCLUDED.format_reply,
                examples = EXCLUDED.examples,
                initial_system = EXCLUDED.initial_system,
                updated_at = NOW()
            RETURNING fabula, bio, style, forbidden, length_hint,
                      goals, format_reply, examples, initial_system, updated_at
            """,
            account_id,
            payload.fabula, payload.bio, payload.style, payload.forbidden,
            payload.length_hint, payload.goals, payload.format_reply,
            payload.examples, payload.initial_system,
        )
    return _row_to_prompts_dict(account_id, row)
