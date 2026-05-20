"""HTTP-ручки модуля lead_evaluator.

    POST /lead-evaluator/dialogs/{id}/evaluate
        Ручной триггер оценки (UI-кнопка "Завершить и оценить").

    GET  /lead-evaluator/dialogs/{id}/evaluation
        Получить сохранённую оценку (для блока "Оценка лида" в карточке).

    GET  /lead-evaluator/prompt
        Получить текущий текст промта оценщика (для редактора).

    PUT  /lead-evaluator/prompt
        Записать новый текст промта (UI-кнопка "Сохранить").
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .errors import LeadEvaluatorError
from .prompts import PROMPT_FILE

router = APIRouter(prefix="/lead-evaluator", tags=["lead_evaluator"])


def _err(e: LeadEvaluatorError) -> HTTPException:
    return HTTPException(
        status_code=e.http_status,
        detail={"error": {"code": e.code, "message": str(e) or e.code}},
    )


def _service(request: Request):
    return request.app.state.lead_evaluator_service


# ─── Pydantic ────────────────────────────────────────────────────────

class PromptUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)


# ─── Endpoints ───────────────────────────────────────────────────────

@router.post("/dialogs/{dialog_id}/evaluate")
async def evaluate_dialog(dialog_id: int, request: Request):
    try:
        result = await _service(request).evaluate(dialog_id, source="manual")
    except LeadEvaluatorError as e:
        raise _err(e)
    return result


@router.get("/dialogs/{dialog_id}/evaluation")
async def get_evaluation(dialog_id: int, request: Request):
    result = await _service(request).get_evaluation(dialog_id)
    if result is None:
        return {"evaluation": None}
    return {"evaluation": result}


@router.get("/prompt")
async def get_prompt():
    try:
        text = PROMPT_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    return {"path": str(PROMPT_FILE), "content": text}


@router.put("/prompt")
async def put_prompt(payload: PromptUpdate):
    PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROMPT_FILE.write_text(payload.content, encoding="utf-8")
    return {"path": str(PROMPT_FILE), "bytes_written": len(payload.content)}
