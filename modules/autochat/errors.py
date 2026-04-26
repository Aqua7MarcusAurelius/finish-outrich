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
    code = "CANNOT_WRITE"
    message = "Не удалось отправить сообщение (privacy/blocked/прочее)"
    status_code = 409


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
