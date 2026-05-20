"""Исключения модуля lead_evaluator."""
from __future__ import annotations


class LeadEvaluatorError(Exception):
    code = "LEAD_EVALUATOR_ERROR"
    http_status = 500


class DialogNotFound(LeadEvaluatorError):
    code = "DIALOG_NOT_FOUND"
    http_status = 404


class PromptNotFound(LeadEvaluatorError):
    code = "EVALUATOR_PROMPT_NOT_FOUND"
    http_status = 503


class LlmCallFailed(LeadEvaluatorError):
    code = "EVALUATOR_LLM_FAILED"
    http_status = 502


class EvaluationAlreadyRunning(LeadEvaluatorError):
    """Защита от двойного запуска на один и тот же диалог."""
    code = "EVALUATION_ALREADY_RUNNING"
    http_status = 409
