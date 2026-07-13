"""
Endpoint'ы модуля авторизации. См. PROJECT_SPEC.md §9 (Авторизация и аккаунты).

Контракт `/auth/start`: оператор передаёт ВСЕ поля fingerprint'а аккаунта
(api_id, api_hash, device_model, system_version, app_version, lang_code,
system_lang_code, lang_pack) плюс опциональный 2FA-пароль. Если Telegram
запросит 2FA — backend сам подставит пароль (без отдельного /auth/2fa),
и оператор получит готовый аккаунт после /auth/code.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from modules.auth.service import AuthError, AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


def get_service(request: Request) -> AuthService:
    return request.app.state.auth_service


class AuthStartIn(BaseModel):
    phone: str = Field(min_length=5, max_length=20)
    name: str = Field(min_length=1, max_length=200)
    proxy_primary: str
    proxy_fallback: str
    api_id: int = Field(gt=0)
    api_hash: str = Field(min_length=1)
    device_model: str = Field(min_length=1, max_length=200)
    system_version: str = Field(min_length=1, max_length=200)
    app_version: str = Field(min_length=1, max_length=200)
    lang_code: str = Field(default="en", min_length=1, max_length=20)
    system_lang_code: str = Field(default="en", min_length=1, max_length=20)
    lang_pack: str = Field(default="", max_length=50)
    # 2FA-пароль вперёд. Backend применит его после /auth/code, если Telegram
    # запросит 2FA. Если 2FA не требуется — поле игнорируется.
    password: str | None = None


class CodeIn(BaseModel):
    session_id: str
    code: str = Field(min_length=1, max_length=20)


class PasswordIn(BaseModel):
    session_id: str
    password: str = Field(min_length=1)


class ReauthIn(BaseModel):
    account_id: int


def _err(e: AuthError) -> JSONResponse:
    return JSONResponse(
        status_code=e.status_code,
        content={"error": {"code": e.code, "message": e.message}},
    )


@router.post("/start")
async def auth_start(payload: AuthStartIn, service: AuthService = Depends(get_service)):
    try:
        return await service.start(
            phone=payload.phone,
            name=payload.name,
            proxy_primary=payload.proxy_primary,
            proxy_fallback=payload.proxy_fallback,
            api_id=payload.api_id,
            api_hash=payload.api_hash,
            device_model=payload.device_model,
            system_version=payload.system_version,
            app_version=payload.app_version,
            lang_code=payload.lang_code,
            system_lang_code=payload.system_lang_code,
            lang_pack=payload.lang_pack,
            password=payload.password,
        )
    except AuthError as e:
        return _err(e)


@router.post("/code")
async def auth_code(payload: CodeIn, service: AuthService = Depends(get_service)):
    try:
        return await service.submit_code(payload.session_id, payload.code)
    except AuthError as e:
        return _err(e)


@router.post("/2fa")
async def auth_2fa(payload: PasswordIn, service: AuthService = Depends(get_service)):
    try:
        return await service.submit_password(payload.session_id, payload.password)
    except AuthError as e:
        return _err(e)


@router.get("/status/{session_id}")
async def auth_status(session_id: str, service: AuthService = Depends(get_service)):
    try:
        return await service.get_status(session_id)
    except AuthError as e:
        return _err(e)


@router.delete("/{session_id}")
async def auth_cancel(session_id: str, service: AuthService = Depends(get_service)):
    await service.cancel(session_id)
    return Response(status_code=204)


@router.post("/reauth")
async def auth_reauth(payload: ReauthIn, service: AuthService = Depends(get_service)):
    try:
        return await service.start_reauth(account_id=payload.account_id)
    except AuthError as e:
        return _err(e)