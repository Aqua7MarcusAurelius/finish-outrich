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

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import db
from modules.autochat.generation import render_preview_text

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
# Per-worker промты для AutoChat (free-form templates)
# ─────────────────────────────────────────────────────────────────────

class PromptsIn(BaseModel):
    initial_template: str = Field(default="", max_length=50000)
    reply_template: str = Field(default="", max_length=50000)


def _account_not_found() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": {"code": "ACCOUNT_NOT_FOUND", "message": "Аккаунт не найден"}},
    )


def _row_to_prompts_dict(account_id: int, row) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "initial_template": row["initial_template"],
        "reply_template": row["reply_template"],
        "updated_at": _iso(row["updated_at"]),
    }


@router.get("/accounts/{account_id}/prompts")
async def get_account_prompts(account_id: int):
    """
    Возвращает per-worker промт-конфиг (два свободных текста).

    Если строки в `account_prompts` ещё нет — оба поля пустые,
    `updated_at = null`. Никаких overlay-дефолтов: оператор сам пишет
    что хочет, дефолт-каркас сейчас не предусмотрен.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        acc = await conn.fetchval("SELECT id FROM accounts WHERE id = $1", account_id)
        if acc is None:
            return _account_not_found()
        row = await conn.fetchrow(
            """
            SELECT initial_template, reply_template, updated_at
            FROM account_prompts WHERE account_id = $1
            """,
            account_id,
        )
    if row is None:
        return {
            "account_id": account_id,
            "initial_template": "",
            "reply_template": "",
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
                account_id, initial_template, reply_template, updated_at
            ) VALUES ($1, $2, $3, NOW())
            ON CONFLICT (account_id) DO UPDATE SET
                initial_template = EXCLUDED.initial_template,
                reply_template = EXCLUDED.reply_template,
                updated_at = NOW()
            RETURNING initial_template, reply_template, updated_at
            """,
            account_id, payload.initial_template, payload.reply_template,
        )
    return _row_to_prompts_dict(account_id, row)


# ─────────────────────────────────────────────────────────────────────
# Preview — что уйдёт в LLM при текущих (несохранённых) полях
# ─────────────────────────────────────────────────────────────────────

class PreviewIn(BaseModel):
    initial_template: str = Field(default="", max_length=50000)
    reply_template: str = Field(default="", max_length=50000)
    # Опционально — какой диалог использовать как источник истории
    # и данных собеседника. None → плейсхолдеры partner_* пустые,
    # conversation_history с маркером.
    dialog_id: int | None = None


@router.post("/accounts/{account_id}/prompts/preview")
async def preview_prompts(account_id: int, payload: PreviewIn):
    """
    Собирает initial и reply system-промты ровно как уйдут в LLM, но
    БЕЗ вызова LLM. Используется редактором промта.

    Если dialog_id указан — подставляются партнёр/история/статистика
    из этого диалога. Если нет — partner_* пустые, conversation_history
    с placeholder-маркером, messages_count/days_since_first останутся
    литералом (видно что в живом запуске они подставятся).
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        acc = await conn.fetchval("SELECT id FROM accounts WHERE id = $1", account_id)
        if acc is None:
            return _account_not_found()
        if payload.dialog_id is not None:
            dlg = await conn.fetchrow(
                "SELECT id FROM dialogs WHERE id = $1 AND account_id = $2",
                payload.dialog_id, account_id,
            )
            if dlg is None:
                return JSONResponse(
                    status_code=400,
                    content={"error": {
                        "code": "DIALOG_NOT_FOUND",
                        "message": "Диалог не найден или принадлежит другому аккаунту",
                    }},
                )

    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        initial_text = await render_preview_text(
            conn,
            template=payload.initial_template,
            account_id=account_id,
            dialog_id=payload.dialog_id,
            now=now,
        )
        reply_text = await render_preview_text(
            conn,
            template=payload.reply_template,
            account_id=account_id,
            dialog_id=payload.dialog_id,
            now=now,
        )

    return {
        "initial": initial_text,
        "reply": reply_text,
        "dialog_id": payload.dialog_id,
    }
