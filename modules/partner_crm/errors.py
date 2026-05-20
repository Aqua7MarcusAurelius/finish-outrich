"""Исключения модуля partner_crm.

Все наследуют PartnerCrmError → у каждого есть свой code и http_status,
который роутер мапит в `{error: {code, message}}` JSON-ответ.
"""
from __future__ import annotations


class PartnerCrmError(Exception):
    """Базовое исключение модуля."""
    code = "PARTNER_CRM_ERROR"
    http_status = 500


class NotConfigured(PartnerCrmError):
    """В .env не заполнены CRM_BASE_URL или CRM_API_TOKEN."""
    code = "PARTNER_CRM_NOT_CONFIGURED"
    http_status = 503


class CrmHttpError(PartnerCrmError):
    """CRM ответила 4xx/5xx или сеть упала."""
    code = "PARTNER_CRM_HTTP_ERROR"
    http_status = 502

    def __init__(self, message: str, *, status: int | None = None,
                 body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class InvalidStatus(PartnerCrmError):
    """Пытаемся послать в CRM значение которого она не принимает."""
    code = "PARTNER_CRM_INVALID_STATUS"
    http_status = 400


# Допустимые финальные значения в CRM (см. docs/INT_CRM_API_TOKEN_GUIDE.md).
PARTNER_TERMINAL_STATUSES = frozenset({
    "ready_for_transfer",
    "invalid",
    "no_response",
})
