"""
HTTP-ручки модуля worker_auto_runner.

    GET    /workers/{id}/auto-runner          — текущая конфигурация + state
    PATCH  /workers/{id}/auto-runner          — обновить queue / interval / jitter
    POST   /workers/{id}/auto-runner/start    — Play
    POST   /workers/{id}/auto-runner/stop     — Pause
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .service import AutoRunnerError, MAX_QUEUE_LEN, MIN_INTERVAL_SEC

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
    return {
        "account_id": int(row["account_id"]),
        "is_running": bool(row["is_running"]),
        "queue": list(row["queue"] or []),
        "interval_sec": int(row["interval_sec"]),
        "jitter_sec": int(row["jitter_sec"]),
        "last_action_at": _iso(row.get("last_action_at")),
        "last_status": row.get("last_status"),
        "updated_at": _iso(row.get("updated_at")),
    }


# ─── Pydantic ────────────────────────────────────────────────────────

class AutoRunnerPatch(BaseModel):
    queue: Optional[list[str]] = Field(default=None, max_length=MAX_QUEUE_LEN)
    interval_sec: Optional[int] = Field(default=None, ge=MIN_INTERVAL_SEC)
    jitter_sec: Optional[int] = Field(default=None, ge=0)


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
            interval_sec=payload.interval_sec,
            jitter_sec=payload.jitter_sec,
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
