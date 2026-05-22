"""
Иерархия ошибок модуля AutoChat.

Роутер маппит их в {"error": {"code", "message"}} ответы API —
симметрично modules/auth/routes.py::_err и modules/worker_manager.
"""
from __future__ import annotations


class AutoChatError(Exception):
    code: str = "AUTOCHAT_ERROR"
    message: str = "ошибка автодиалога"
    status_code: int = 400

    def __init__(self, message: str | None = None):
        self.message = message or self.__class__.message or self.code
        super().__init__(self.message)


class AccountNotFound(AutoChatError):
    code = "ACCOUNT_NOT_FOUND"
    message = "Аккаунт не найден"
    status_code = 404


class WorkerNotRunning(AutoChatError):
    code = "WORKER_NOT_RUNNING"
    message = "Воркер аккаунта не запущен"
    status_code = 409


class SessionAlreadyActive(AutoChatError):
    code = "SESSION_ALREADY_ACTIVE"
    message = "На этого собеседника уже есть активная автосессия"
    status_code = 409


class UsernameNotFoundError(AutoChatError):
    code = "USERNAME_NOT_FOUND"
    message = "Такой @username не найден"
    status_code = 404


class UsernameUnavailableError(AutoChatError):
    code = "USERNAME_UNAVAILABLE"
    message = "@username не доступен для написания"
    status_code = 400


class CannotWrite(AutoChatError):
    """
    Лид физически недоступен для написания: privacy, заблокировал,
    Premium-only, и т.п. Категория «лид невалиден» для CRM-флоу.
    НЕ используется для транзиентных проблем (см. FloodWait /
    SendFailedTransient ниже).
    """
    code = "CANNOT_WRITE"
    message = "Лид недоступен для написания (privacy / блок / Premium-only)"
    status_code = 409


class FloodWait(AutoChatError):
    """
    Telegram дросселит наши исходящие. Транзиент со стороны нашего
    воркера — НЕ ошибка лида. CRM-флоу должен реинзертить лида в пул
    и останавливать воркер.
    """
    code = "FLOOD_WAIT"
    message = "TG ограничил исходящие сообщения — нужно подождать"
    status_code = 429

    def __init__(self, *, seconds: int | None = None, message: str | None = None):
        self.seconds = seconds
        super().__init__(message)


class SendFailedTransient(AutoChatError):
    """
    Прочая транзиентная сетевая / RPC ошибка при `send_message`.
    Безопасный дефолт для «всего непонятного» — лучше пустить лида
    через транзиент, чем ошибочно закрыть как invalid у партнёра.
    """
    code = "SEND_FAILED_TRANSIENT"
    message = "Не удалось отправить (сеть/RPC) — попробуем позже"
    status_code = 502


class GenerationFailed(AutoChatError):
    code = "GENERATION_FAILED"
    message = "Не удалось сгенерировать сообщение через Opus"
    status_code = 502


class SessionNotFound(AutoChatError):
    code = "SESSION_NOT_FOUND"
    message = "Сессия автодиалога не найдена"
    status_code = 404


class SessionExpired(AutoChatError):
    code = "SESSION_EXPIRED"
    message = "Сессия Telegram протухла — нужна переавторизация"
    status_code = 410


class PromptNotConfigured(AutoChatError):
    code = "PROMPT_NOT_CONFIGURED"
    message = "У воркера не задан initial_system промт — заполни его в редакторе на карточке аккаунта"
    status_code = 409


class DialogNotFound(AutoChatError):
    code = "DIALOG_NOT_FOUND"
    message = "Диалог не найден"
    status_code = 404


class NoActiveSession(AutoChatError):
    """Нет активной autochat-сессии для этого диалога — нечего регенерить."""
    code = "NO_ACTIVE_SESSION"
    message = "На этом диалоге нет активной авто-сессии. Включи автодиалог сначала."
    status_code = 409
