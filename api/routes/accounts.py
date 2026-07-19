"""
GET /accounts — список аккаунтов для страницы «Диалоги».

По контракту (docs/ui/web_ui_api_contract_v1.md): `worker.list` с добавленными
`dialogs_count` (из БД) и `last_event_at` (из events_archive).

Роут живёт здесь, а не в worker_manager, потому что это чисто read-модель
для UI — она агрегирует данные из worker_manager, истории и шины.

Также тут CRUD для per-worker промтов (account_prompts) — редактор в UI
вызывает GET/PUT /accounts/{id}/prompts. Три свободных текста:
  - initial_template  — autochat, первое сообщение (пустой блокирует
    POST /autochat/start);
  - reply_template    — autochat, ответы в переписке (пустой блокирует
    генерацию, см. session.py);
  - evaluator_template — lead_evaluator, промт оценщика диалога. Пустой =
    используется глобальный `prompts/lead_evaluator_system.md` (см.
    modules/lead_evaluator/prompts.py). Даёт разным семействам воркеров
    свой оценщик с разными критериями «успеха».
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import db
from modules.autochat.generation import render_preview_text
from modules.history.wipe import wipe_all_dialogs

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
        # Lead-настройки (10.9): источник и фильтр пола.
        lead_settings = {
            r["id"]: (r["lead_source"], r["target_gender"])
            for r in await conn.fetch(
                "SELECT id, lead_source, target_gender FROM accounts "
                "WHERE id = ANY($1::int[])",
                ids,
            )
        }

    out: list[dict[str, Any]] = []
    for w in workers:
        acc_id = w["account_id"]
        ls = lead_settings.get(acc_id, ("manual", "any"))
        out.append({
            "id": acc_id,
            "name": w["name"],
            "phone": w["phone"],
            "status": w["status"],
            "is_active": w["is_active"],
            "dialogs_count": dialog_counts.get(acc_id, 0),
            "last_event_at": _iso(last_event_times.get(acc_id)),
            "lead_source": ls[0],
            "target_gender": ls[1],
        })
    return out


# ─────────────────────────────────────────────────────────────────────
# Lead-settings: источник лидов и фильтр пола (10.9)
# ─────────────────────────────────────────────────────────────────────

class LeadSettingsPatch(BaseModel):
    lead_source: str | None = Field(default=None, pattern=r"^(manual|partner_crm)$")
    target_gender: str | None = Field(default=None, pattern=r"^(any|female|male)$")


@router.patch("/accounts/{account_id}/lead-settings")
async def patch_lead_settings(account_id: int, payload: LeadSettingsPatch):
    """
    Менять lead_source / target_gender аккаунта.

    - lead_source: 'manual' (свой queue) | 'partner_crm' (общий пул)
    - target_gender: 'any' | 'female' | 'male' (фильтр при подборе из пула)

    Хотя бы одно поле должно быть передано.
    """
    if payload.lead_source is None and payload.target_gender is None:
        return JSONResponse(
            status_code=400,
            content={"error": {
                "code": "EMPTY_PATCH",
                "message": "Передайте хотя бы одно поле для обновления",
            }},
        )

    pool = db.get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM accounts WHERE id = $1", account_id,
        )
        if not exists:
            return _account_not_found()

        sets: list[str] = ["updated_at = NOW()"]
        params: list[Any] = [account_id]
        if payload.lead_source is not None:
            params.append(payload.lead_source)
            sets.append(f"lead_source = ${len(params)}")
        if payload.target_gender is not None:
            params.append(payload.target_gender)
            sets.append(f"target_gender = ${len(params)}")

        row = await conn.fetchrow(
            f"UPDATE accounts SET {', '.join(sets)} "
            f"WHERE id = $1 RETURNING id, lead_source, target_gender",
            *params,
        )
    return dict(row)


# ─────────────────────────────────────────────────────────────────────
# Per-worker промты для AutoChat (free-form templates)
# ─────────────────────────────────────────────────────────────────────

class PromptsIn(BaseModel):
    initial_template: str = Field(default="", max_length=50000)
    reply_template: str = Field(default="", max_length=50000)
    # Пустой = глобальный оценщик по умолчанию (prompts/lead_evaluator_system.md).
    evaluator_template: str = Field(default="", max_length=50000)


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
        "evaluator_template": row["evaluator_template"],
        "updated_at": _iso(row["updated_at"]),
    }


@router.get("/accounts/{account_id}/prompts")
async def get_account_prompts(account_id: int):
    """
    Возвращает per-worker промт-конфиг (два свободных текста).

    Если строки в `account_prompts` ещё нет — все поля пустые,
    `updated_at = null`. Никаких overlay-дефолтов: оператор сам пишет
    что хочет, дефолт-каркас сейчас не предусмотрен. Пустой
    `evaluator_template` означает «оценщик по глобальному промту».
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        acc = await conn.fetchval("SELECT id FROM accounts WHERE id = $1", account_id)
        if acc is None:
            return _account_not_found()
        row = await conn.fetchrow(
            """
            SELECT initial_template, reply_template, evaluator_template, updated_at
            FROM account_prompts WHERE account_id = $1
            """,
            account_id,
        )
    if row is None:
        return {
            "account_id": account_id,
            "initial_template": "",
            "reply_template": "",
            "evaluator_template": "",
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
                account_id, initial_template, reply_template,
                evaluator_template, updated_at
            ) VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (account_id) DO UPDATE SET
                initial_template = EXCLUDED.initial_template,
                reply_template = EXCLUDED.reply_template,
                evaluator_template = EXCLUDED.evaluator_template,
                updated_at = NOW()
            RETURNING initial_template, reply_template,
                      evaluator_template, updated_at
            """,
            account_id, payload.initial_template, payload.reply_template,
            payload.evaluator_template,
        )
    return _row_to_prompts_dict(account_id, row)


# ─────────────────────────────────────────────────────────────────────
# Preview — что уйдёт в LLM при текущих (несохранённых) полях
# ─────────────────────────────────────────────────────────────────────

class PreviewIn(BaseModel):
    initial_template: str = Field(default="", max_length=50000)
    reply_template: str = Field(default="", max_length=50000)
    evaluator_template: str = Field(default="", max_length=50000)
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
        evaluator_text = await render_preview_text(
            conn,
            template=payload.evaluator_template,
            account_id=account_id,
            dialog_id=payload.dialog_id,
            now=now,
        )

    return {
        "initial": initial_text,
        "reply": reply_text,
        "evaluator": evaluator_text,
        "dialog_id": payload.dialog_id,
    }


# ─────────────────────────────────────────────────────────────────────
# Bulk wipe: снести все диалоги аккаунта (Telegram + локально)
# ─────────────────────────────────────────────────────────────────────

@router.post("/accounts/{account_id}/dialogs/wipe")
async def wipe_account_dialogs(
    account_id: int,
    request: Request,
    confirm: str = "",
    revoke: bool = True,
):
    """
    Удалить все диалоги аккаунта:
      - на стороне Telegram через wrapper.delete_dialog(revoke=...).
        revoke=True (по умолчанию) — стирает историю и у собеседников.
      - локально через purge_dialog_locally (тот же flow, что у штучного
        DELETE /dialogs/{id}: CASCADE по messages/media/reactions/edits,
        чистка MinIO, остановка autochat-сессии, dialog.deleted в шину).

    Защита: query-параметр `confirm` ДОЛЖЕН быть равен `CONFIRM`. UI
    дополнительно требует ввести эту строку руками в модалке. Бэк
    проверяет независимо — обход UI через curl не сработает.

    Воркер должен быть запущен — иначе нечем удалять на стороне Telegram.
    На длинном FloodWait операция останавливается с partial result, чтобы
    оператор повторил запрос позже (см. modules/history/wipe.py).
    """
    try:
        result = await wipe_all_dialogs(
            account_id, request=request, confirm=confirm, revoke=revoke,
        )
    except ValueError as e:
        code = str(e)
        status_code = {
            "INVALID_CONFIRM": 400,
            "ACCOUNT_NOT_FOUND": 404,
            "WORKER_NOT_RUNNING": 409,
        }.get(code, 400)
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": code, "message": code.replace("_", " ").lower()}},
        )
    return result
