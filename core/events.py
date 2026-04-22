"""
Константы для событий шины.

Используем вместо магических строк по коду — легче искать, сложнее опечататься.
Список соответствует docs/event_bus.md.
"""
from __future__ import annotations


class Module:
    """Кто публикует событие."""
    HISTORY = "history"
    HISTORY_SYNC = "history_sync"
    TRANSCRIPTION = "transcription"
    DESCRIPTION = "description"
    WRAPPER = "wrapper"
    WORKER = "worker"
    WORKER_MANAGER = "worker_manager"
    AUTH = "auth"
    CLEANER = "cleaner"
    API = "api"
    SYSTEM = "system"
    BUS = "bus"
    AUTOCHAT = "autochat"


class Status:
    """Результат события."""
    SUCCESS = "success"
    ERROR = "error"
    IN_PROGRESS = "in_progress"


class EventType:
    """Типы событий. Формат: <домен>.<глагол в прошедшем времени>."""

    # ── Сообщения ────────────────────────────────────────────────────
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_SAVED = "message.saved"
    MESSAGE_UPDATED = "message.updated"
    MESSAGE_REACTED = "message.reacted"

    # ── Медиа ────────────────────────────────────────────────────────
    TRANSCRIPTION_STARTED = "transcription.started"
    TRANSCRIPTION_DONE = "transcription.done"
    DESCRIPTION_STARTED = "description.started"
    DESCRIPTION_DONE = "description.done"
    MEDIA_REPROCESS_REQUESTED = "media.reprocess.requested"
    FILE_CLEANED = "file.cleaned"

    # ── Аккаунты ─────────────────────────────────────────────────────
    ACCOUNT_CREATED = "account.created"
    ACCOUNT_REAUTHORIZED = "account.reauthorized"
    ACCOUNT_SESSION_EXPIRED = "account.session_expired"
    ACCOUNT_DELETED = "account.deleted"

    # ── Воркеры ──────────────────────────────────────────────────────
    WORKER_STARTED = "worker.started"
    WORKER_STOPPED = "worker.stopped"
    WORKER_CRASHED = "worker.crashed"

    # ── Нагон истории ────────────────────────────────────────────────
    SYNC_STARTED = "sync.started"
    SYNC_DIALOG_DONE = "sync.dialog.done"
    SYNC_DONE = "sync.done"

    # ── AutoChat (автодиалоги через Opus 4.7) ────────────────────────
    AUTOCHAT_STARTED = "autochat.started"
    AUTOCHAT_INITIAL_SENT = "autochat.initial_sent"
    AUTOCHAT_ENTERED_CHAT = "autochat.entered_chat"
    AUTOCHAT_LEFT_CHAT = "autochat.left_chat"
    AUTOCHAT_GENERATION_REQUESTED = "autochat.generation_requested"
    AUTOCHAT_GENERATION_DONE = "autochat.generation_done"
    AUTOCHAT_SEGMENT_SENT = "autochat.segment_sent"
    AUTOCHAT_SESSION_STOPPED = "autochat.session_stopped"
    AUTOCHAT_SESSION_ERROR = "autochat.session_error"

    # Тайпинг собеседника — публикует враппер, слушает AutoChat
    DIALOG_TYPING_OBSERVED = "dialog.typing_observed"

    # ── Системные ────────────────────────────────────────────────────
    SYSTEM_ERROR = "system.error"
