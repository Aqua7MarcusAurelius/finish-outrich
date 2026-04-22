"""
Endpoints модуля AutoChat. См. docs/api.md → раздел "AutoChat".

    POST   /autochat/start              — создать и запустить сессию
    GET    /autochat/sessions           — список (фильтры: account_id, status)
    GET    /autochat/sessions/{id}      — одна сессия
    POST   /autochat/sessions/{id}/stop — остановить
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import AutoChatError
from .service import AutoChatService

router = APIRouter(prefix="/autochat", tags=["autochat"])


def _service(request: Request) -> AutoChatService:
    return request.app.state.autochat_service


def _err(e: AutoChatError) -> JSONResponse:
    return JSONResponse(
        status_code=e.status_code,
        content={"error": {"code": e.code, "message": e.message}},
    )


# ─── Pydantic-модели запросов ─────────────────────────────────────────

class StartIn(BaseModel):
    account_id: int
    username: str = Field(min_length=1, max_length=64)
    system_prompt: str = Field(min_length=1, max_length=20000)
    initial_prompt: str = Field(min_length=1, max_length=10000)


# ─── Endpoints ────────────────────────────────────────────────────────

@router.post("/start")
async def autochat_start(payload: StartIn, request: Request):
    service = _service(request)
    try:
        session = await service.create_session(
            account_id=payload.account_id,
            username=payload.username,
            system_prompt=payload.system_prompt,
            initial_prompt=payload.initial_prompt,
        )
        return {"session": session}
    except AutoChatError as e:
        return _err(e)


@router.get("/sessions")
async def autochat_list(
    request: Request,
    account_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
):
    service = _service(request)
    items = await service.list_sessions(account_id=account_id, status=status)
    return {"sessions": items}


@router.get("/sessions/{session_id}")
async def autochat_get(session_id: int, request: Request):
    service = _service(request)
    try:
        session = await service.get_session(session_id)
        return {"session": session}
    except AutoChatError as e:
        return _err(e)


@router.post("/sessions/{session_id}/stop")
async def autochat_stop(session_id: int, request: Request):
    service = _service(request)
    try:
        session = await service.stop_session(session_id)
        return {"session": session}
    except AutoChatError as e:
        return _err(e)
