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
    DIALOG_STATUSES = "dialog_statuses"
    WORKER_AUTO_RUNNER = "worker_auto_runner"
    PEER_PROFILE = "peer_profile"
    PARTNER_CRM = "partner_crm"
    LEAD_EVALUATOR = "lead_evaluator"


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

    # ── Диалоги ──────────────────────────────────────────────────────
    # Новая строка в dialogs появилась впервые (history.service после INSERT,
    # не UPDATE). Слушает peer_profile — чтобы один раз дотянуть bio+birthday
    # через GetFullUserRequest. UI/CRM-export могут подписаться отдельно.
    DIALOG_CREATED = "dialog.created"
    # Расширенный профиль успешно подтянут — публикует peer_profile, слушает
    # history (UPDATE dialogs.bio, birthday_*).
    DIALOG_PROFILE_FETCHED = "dialog.profile.fetched"
    DIALOG_DELETED = "dialog.deleted"
    # Завершение bulk-операции «снести все диалоги аккаунта»
    # (POST /accounts/{id}/dialogs/wipe). Каждый успешно удалённый диалог
    # шлёт свой `dialog.deleted`; это — итоговый агрегат на всю операцию.
    ACCOUNT_DIALOGS_WIPED = "account.dialogs_wiped"
    # Операторская «пометка» (status_id) на диалоге изменилась.
    # source: "manual" | "event:<type>" | др. — задел под автоматизацию,
    # когда статус будет проставляться listener'ом событий шины.
    DIALOG_STATUS_CHANGED = "dialog.status_changed"
    # CRUD над каталогом dialog_statuses (создан/обновлён/удалён пузырь).
    # Один тип на все три операции — UI слушает и инвалидит кэш.
    DIALOG_STATUSES_CATALOG_CHANGED = "dialog_statuses.catalog_changed"

    # ── Worker Auto Runner (автомат старта диалогов из очереди юзернеймов) ──
    # Это и есть «лог автомата» — отдельной таблицы для логов нет, всё
    # читается из events_archive с фильтром module=worker_auto_runner.
    AUTO_RUNNER_STARTED = "auto_runner.started"
    AUTO_RUNNER_STOPPED = "auto_runner.stopped"
    AUTO_RUNNER_LEAD_SENT = "auto_runner.lead_sent"
    AUTO_RUNNER_LEAD_SKIPPED = "auto_runner.lead_skipped"
    AUTO_RUNNER_QUEUE_EMPTY = "auto_runner.queue_empty"

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
    # Сгенерировать ответ нечем — у воркера пустой reply_system промт.
    # Сессия не валится, ждёт пока пользователь заполнит промт в UI.
    AUTOCHAT_GENERATION_SKIPPED = "autochat.generation_skipped"
    # LLM решила что цели разговора достигнуты — поставила маркер
    # <finishdialog/>. После отправки последнего сегмента сессия гасится.
    AUTOCHAT_FINISHED_BY_LLM = "autochat.finished_by_llm"
    # HTML-отчёт по сессии успешно/неуспешно сохранён в MinIO.
    AUTOCHAT_REPORT_SAVED = "autochat.report_saved"
    AUTOCHAT_REPORT_FAILED = "autochat.report_failed"

    # Тайпинг собеседника — публикует враппер, слушает AutoChat
    DIALOG_TYPING_OBSERVED = "dialog.typing_observed"

    # ── Партнёрский CRM (этап 10) ────────────────────────────────────
    # Сторож пула докачал лидов из CRM.
    PARTNER_CRM_POOL_REFILLED = "partner_crm.pool_refilled"
    # Не смогли дозалить пул — сетевая ошибка / 4xx от CRM.
    PARTNER_CRM_REFILL_FAILED = "partner_crm.refill_failed"
    # Воркер забрал лид из общего пула и идёт стартовать autochat.
    PARTNER_CRM_LEAD_TAKEN = "partner_crm.lead_taken"
    # Финал отправлен в CRM (POST /leads/:u/status).
    PARTNER_CRM_STATUS_PUSHED = "partner_crm.status_pushed"
    PARTNER_CRM_STATUS_PUSH_FAILED = "partner_crm.status_push_failed"
    # Лид "поставлен в работу" (POST /leads/:u/start) — autochat начал.
    PARTNER_CRM_LEAD_STARTED = "partner_crm.lead_started"
    PARTNER_CRM_LEAD_START_FAILED = "partner_crm.lead_start_failed"

    # ── Lead Evaluator (этап 10) ─────────────────────────────────────
    # Оценщик начал смотреть диалог.
    LEAD_EVALUATOR_STARTED = "lead_evaluator.started"
    # Оценщик выдал вердикт (success | archive) + извлечённые поля.
    LEAD_EVALUATOR_COMPLETED = "lead_evaluator.completed"
    # LLM не дал распарсить ответ даже после ретрая. Считаем archive,
    # отчёт уйдёт в папку archive/ для ручного разбора.
    LEAD_EVALUATOR_PARSE_FAILED = "lead_evaluator.parse_failed"

    # ── Системные ────────────────────────────────────────────────────
    SYSTEM_ERROR = "system.error"
