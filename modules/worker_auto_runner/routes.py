"""
HTTP-ручки модуля worker_auto_runner.

    GET    /workers/{id}/auto-runner          — текущая конфигурация + state
    PATCH  /workers/{id}/auto-runner          — обновить queue / window / N-леды / warmup
    POST   /workers/{id}/auto-runner/start    — Play (стартует сессию + расписание)
    POST   /workers/{id}/auto-runner/stop     — Pause (гасит сессию)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .service import (
    AutoRunnerError,
    MAX_LEADS_UPPER,
    MAX_QUEUE_LEN,
    MAX_WARMUP_MINUTES,
    MAX_WINDOW_HOURS,
)

router = APIRouter(tags=["worker_auto_runner"])


def _err(e: AutoRunnerError) -> HTTPException:
    return HTTPException(
        status_code=e.http_status,
        detail={"error": {"code": e.code, "message": str(e) or e.code}},
    )


def _service(request: Request):
    return request.app.state.auto_runner_service


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _to_dto(row: dict[str, Any]) -> dict[str, Any]:
    """Преобразуем raw row в JSON-friendly формат для UI."""
    schedule = [_iso(t) for t in (row.get("schedule") or [])]
    return {
        "account_id": int(row["account_id"]),
        "is_running": bool(row["is_running"]),
        "queue": list(row["queue"] or []),
        "window_hours": int(row["window_hours"]),
        "min_leads": int(row["min_leads"]),
        "max_leads": int(row["max_leads"]),
        "warmup_minutes": int(row["warmup_minutes"]),
        "session_started_at": _iso(row.get("session_started_at")),
        "session_ends_at": _iso(row.get("session_ends_at")),
        "schedule": schedule,
        "schedule_remaining": len(schedule),
        "next_dispatch_at": schedule[0] if schedule else None,
        "active_start_hour_msk": int(row["active_start_hour_msk"]),
        "active_end_hour_msk": int(row["active_end_hour_msk"]),
        "cooldown_until": _iso(row.get("cooldown_until")),
        "cooldown_reason": row.get("cooldown_reason"),
        "last_action_at": _iso(row.get("last_action_at")),
        "last_status": row.get("last_status"),
        "updated_at": _iso(row.get("updated_at")),
    }


# ─── Pydantic ────────────────────────────────────────────────────────

class AutoRunnerPatch(BaseModel):
    queue: Optional[list[str]] = Field(default=None, max_length=MAX_QUEUE_LEN)
    window_hours: Optional[int] = Field(default=None, ge=1, le=MAX_WINDOW_HOURS)
    min_leads: Optional[int] = Field(default=None, ge=1, le=MAX_LEADS_UPPER)
    max_leads: Optional[int] = Field(default=None, ge=1, le=MAX_LEADS_UPPER)
    warmup_minutes: Optional[int] = Field(default=None, ge=0, le=MAX_WARMUP_MINUTES)
    active_start_hour_msk: Optional[int] = Field(default=None, ge=0, le=23)
    active_end_hour_msk: Optional[int] = Field(default=None, ge=1, le=24)


# ─── Endpoints ───────────────────────────────────────────────────────

@router.get("/workers/{account_id}/auto-runner")
async def get_auto_runner(account_id: int, request: Request):
    try:
        row = await _service(request).get_config(account_id)
    except AutoRunnerError as e:
        raise _err(e)
    return _to_dto(row)


@router.patch("/workers/{account_id}/auto-runner")
async def patch_auto_runner(account_id: int, payload: AutoRunnerPatch, request: Request):
    try:
        row = await _service(request).update_config(
            account_id,
            queue=payload.queue,
            window_hours=payload.window_hours,
            min_leads=payload.min_leads,
            max_leads=payload.max_leads,
            warmup_minutes=payload.warmup_minutes,
            active_start_hour_msk=payload.active_start_hour_msk,
            active_end_hour_msk=payload.active_end_hour_msk,
        )
    except AutoRunnerError as e:
        raise _err(e)
    return _to_dto(row)


@router.post("/workers/{account_id}/auto-runner/start")
async def start_auto_runner(account_id: int, request: Request):
    try:
        row = await _service(request).start(account_id)
    except AutoRunnerError as e:
        raise _err(e)
    return _to_dto(row)


@router.post("/workers/{account_id}/auto-runner/stop")
async def stop_auto_runner(account_id: int, request: Request):
    try:
        row = await _service(request).stop(account_id)
    except AutoRunnerError as e:
        raise _err(e)
    return _to_dto(row)
