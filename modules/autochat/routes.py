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


# ─── Endpoints ────────────────────────────────────────────────────────

@router.post("/start")
async def autochat_start(payload: StartIn, request: Request):
    """
    Запускает авто-диалог. Никаких per-session промтов / задач для первого
    сообщения — всё берётся из per-worker `account_prompts.initial_system`
    (см. modules/autochat/prompts.py + страница /workers/{id}/prompt).

    Body: {account_id, username}.
    """
    service = _service(request)
    try:
        session = await service.create_session(
            account_id=payload.account_id,
            username=payload.username,
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


# ─── Toggle для существующего диалога ─────────────────────────────────
# Эти endpoint'ы дёргают AutoChatService напрямую. Регистрируются на
# отдельном роутере без префикса /autochat — чтобы UI обращался
# по естественному RESTful URL: /dialogs/{id}/autochat.

dialog_autochat_router = APIRouter(tags=["autochat"])


@dialog_autochat_router.get("/dialogs/{dialog_id}/autochat")
async def autochat_dialog_status(dialog_id: int, request: Request):
    service = _service(request)
    try:
        return await service.status_for_dialog(dialog_id)
    except AutoChatError as e:
        return _err(e)


@dialog_autochat_router.post("/dialogs/{dialog_id}/autochat")
async def autochat_dialog_enable(dialog_id: int, request: Request):
    """Включить автодиалог для существующего диалога — без отправки initial."""
    service = _service(request)
    try:
        session = await service.enable_for_dialog(dialog_id)
        return {"session": session}
    except AutoChatError as e:
        return _err(e)


@dialog_autochat_router.delete("/dialogs/{dialog_id}/autochat")
async def autochat_dialog_disable(dialog_id: int, request: Request):
    """Выключить активную автодиалог-сессию для диалога."""
    service = _service(request)
    try:
        session = await service.disable_for_dialog(dialog_id)
        return {"session": session}
    except AutoChatError as e:
        return _err(e)
