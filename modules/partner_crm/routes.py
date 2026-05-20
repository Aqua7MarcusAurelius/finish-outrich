"""
HTTP-ручки модуля partner_crm.

    GET  /partner-crm/health           — конфигурирован ли модуль (нужен UI для бэйджа)
    GET  /partner-crm/preferences      — прозрачный прокси GET /api/l1/preferences (для UI)
    GET  /partner-crm/pool             — состояние локального пула (для UI)
    POST /partner-crm/pool/refill      — вручную дозалить пул из CRM (для UI: "Забрать N лидов сейчас")

Эндпоинты подбора (`claim_lead_for_account`) — внутренние, для
worker_auto_runner; через HTTP не выставлены.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .errors import PartnerCrmError

router = APIRouter(prefix="/partner-crm", tags=["partner_crm"])


def _err(e: PartnerCrmError) -> HTTPException:
    return HTTPException(
        status_code=e.http_status,
        detail={"error": {"code": e.code, "message": str(e) or e.code}},
    )


def _service(request: Request):
    return request.app.state.partner_crm_service


# ─── Pydantic ────────────────────────────────────────────────────────

class RefillPayload(BaseModel):
    count: int = Field(ge=1, le=200)


# ─── Endpoints ───────────────────────────────────────────────────────

@router.get("/health")
async def health(request: Request):
    """
    Лёгкая ручка — пингает что модуль сконфигурирован.
    Не делает реальный запрос в CRM (для этого `preferences`).
    """
    svc = _service(request)
    return {
        "configured": svc.is_configured(),
        "pool_size": await svc.pool_size(),
    }


@router.get("/preferences")
async def preferences(request: Request):
    try:
        return await _service(request).get_preferences()
    except PartnerCrmError as e:
        raise _err(e)


@router.get("/pool")
async def list_pool(request: Request, limit: Optional[int] = 200):
    items = await _service(request).list_pool(limit=int(limit or 200))
    return {"items": items, "count": len(items)}


@router.post("/pool/refill")
async def refill_pool(payload: RefillPayload, request: Request):
    try:
        result = await _service(request).refill_pool(payload.count)
    except PartnerCrmError as e:
        raise _err(e)
    return result
