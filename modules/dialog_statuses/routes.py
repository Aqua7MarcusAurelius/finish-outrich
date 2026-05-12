"""
HTTP-ручки модуля dialog_statuses.

  GET    /dialog-statuses           — список статусов (sort_order ASC)
  POST   /dialog-statuses           — создать
  PATCH  /dialog-statuses/{id}      — переименовать/перекрасить/переставить
  DELETE /dialog-statuses/{id}      — удалить (диалоги с этим status_id
                                      получат NULL через FK ON DELETE SET NULL)

  PATCH  /dialogs/{id}/status       — присвоить статус диалогу. Body:
                                      {status_id: int | null}. Переехал
                                      сюда из modules/history/routes.py
                                      (раньше принимал {status: str}).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import service

router = APIRouter(tags=["dialog_statuses"])


def _err(e: service.DialogStatusError) -> HTTPException:
    return HTTPException(
        status_code=e.http_status,
        detail={"error": {"code": e.code, "message": str(e) or e.code}},
    )


# ─── Pydantic-модели ─────────────────────────────────────────────────

class StatusCreate(BaseModel):
    label: str = Field(min_length=1, max_length=32)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    sort_order: Optional[int] = None


class StatusPatch(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=32)
    color: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    sort_order: Optional[int] = None


class DialogStatusAssign(BaseModel):
    # None = снять статус.
    status_id: Optional[int] = None


# ─── Каталог ─────────────────────────────────────────────────────────

@router.get("/dialog-statuses")
async def list_statuses():
    items = await service.list_statuses()
    return {"items": items}


@router.post("/dialog-statuses", status_code=201)
async def create_status(payload: StatusCreate):
    try:
        return await service.create_status(
            label=payload.label,
            color=payload.color,
            sort_order=payload.sort_order,
        )
    except service.DialogStatusError as e:
        raise _err(e)


@router.patch("/dialog-statuses/{status_id}")
async def update_status(status_id: int, payload: StatusPatch):
    try:
        return await service.update_status(
            status_id,
            label=payload.label,
            color=payload.color,
            sort_order=payload.sort_order,
        )
    except service.DialogStatusError as e:
        raise _err(e)


@router.delete("/dialog-statuses/{status_id}", status_code=204)
async def delete_status(status_id: int):
    try:
        await service.delete_status(status_id)
    except service.DialogStatusError as e:
        raise _err(e)
    return None


# ─── Присвоение диалогу ──────────────────────────────────────────────

@router.patch("/dialogs/{dialog_id}/status")
async def set_dialog_status(dialog_id: int, payload: DialogStatusAssign):
    try:
        return await service.set_dialog_status(
            dialog_id, payload.status_id, source="manual",
        )
    except service.DialogStatusError as e:
        raise _err(e)
