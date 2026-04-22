"""
Шаблоны для поля `message` в событиях шины.

Поле `message` не хранится в БД — вычисляется на лету при отдаче через API.
Это позволяет менять формулировки без миграций.

См. docs/event_bus.md (раздел «Просмотр и управление через API»).
"""
from __future__ import annotations

from typing import Any, Callable

from core.events import EventType, Status


# Шаблоны по типу события. Каждая функция принимает data и возвращает фразу.
_TEMPLATES: dict[str, Callable[[dict[str, Any]], str]] = {
    # ── Сообщения ────────────────────────────────────────────────────
    EventType.MESSAGE_RECEIVED: lambda d: (
        f"получено сообщение (tg #{d.get('telegram_message_id', '?')})"
    ),
    EventType.MESSAGE_SAVED: lambda d: (
        f"сообщение #{d.get('message_id', '?')} записано в БД"
    ),
    EventType.MESSAGE_UPDATED: lambda d: (
        f"сообщение #{d.get('message_id', '?')} обновлено"
    ),
    EventType.MESSAGE_REACTED: lambda d: (
        f"реакция на сообщение #{d.get('message_id', '?')}"
    ),

    # ── Медиа ────────────────────────────────────────────────────────
    EventType.TRANSCRIPTION_STARTED: lambda d: (
        f"транскрибация media #{d.get('media_id', '?')} запущена"
    ),
    EventType.TRANSCRIPTION_DONE: lambda d: (
        f"транскрибация media #{d.get('media_id', '?')} завершена"
    ),
    EventType.DESCRIPTION_STARTED: lambda d: (
        f"описание media #{d.get('media_id', '?')} запущено"
    ),
    EventType.DESCRIPTION_DONE: lambda d: (
        f"описание media #{d.get('media_id', '?')} завершено"
    ),
    EventType.MEDIA_REPROCESS_REQUESTED: lambda d: (
        f"запрошена повторная обработка media #{d.get('media_id', '?')}"
    ),
    EventType.FILE_CLEANED: lambda d: (
        f"файл удалён из MinIO (media #{d.get('media_id', '?')})"
    ),

    # ── Аккаунты ─────────────────────────────────────────────────────
    EventType.ACCOUNT_CREATED: lambda d: (
        f"аккаунт создан (id={d.get('account_id', '?')}"
        f"{', ' + d['phone'] if d.get('phone') else ''})"
    ),
    EventType.ACCOUNT_REAUTHORIZED: lambda d: (
        f"аккаунт #{d.get('account_id', '?')} переавторизован"
    ),
    EventType.ACCOUNT_SESSION_EXPIRED: lambda d: (
        f"сессия аккаунта #{d.get('account_id', '?')} протухла"
    ),
    EventType.ACCOUNT_DELETED: lambda d: (
        f"аккаунт #{d.get('account_id', '?')} удалён"
    ),

    # ── Воркеры ──────────────────────────────────────────────────────
    EventType.WORKER_STARTED: lambda d: "воркер запущен",
    EventType.WORKER_STOPPED: lambda d: "воркер остановлен",
    EventType.WORKER_CRASHED: lambda d: (
        f"воркер упал: {d.get('error', 'неизвестная ошибка')}"
    ),

    # ── Нагон ────────────────────────────────────────────────────────
    EventType.SYNC_STARTED: lambda d: "нагон истории запущен",
    EventType.SYNC_DIALOG_DONE: lambda d: (
        f"нагон диалога #{d.get('dialog_id', '?')} завершён "
        f"({d.get('messages_count', 0)} сообщений)"
    ),
    EventType.SYNC_DONE: lambda d: "нагон завершён",

    # ── AutoChat ─────────────────────────────────────────────────────
    EventType.AUTOCHAT_STARTED: lambda d: (
        f"автодиалог #{d.get('session_id', '?')} с @{d.get('username', '?')} запущен"
    ),
    EventType.AUTOCHAT_INITIAL_SENT: lambda d: (
        f"автодиалог #{d.get('session_id', '?')}: отправлено первое сообщение"
    ),
    EventType.AUTOCHAT_ENTERED_CHAT: lambda d: (
        f"автодиалог #{d.get('session_id', '?')}: вошли в чат "
        f"(задержка {d.get('delay_sec', '?')}с)"
    ),
    EventType.AUTOCHAT_LEFT_CHAT: lambda d: (
        f"автодиалог #{d.get('session_id', '?')}: вышли из чата по idle"
    ),
    EventType.AUTOCHAT_GENERATION_REQUESTED: lambda d: (
        f"автодиалог #{d.get('session_id', '?')}: запрос в Opus"
    ),
    EventType.AUTOCHAT_GENERATION_DONE: lambda d: (
        f"автодиалог #{d.get('session_id', '?')}: получен ответ "
        f"({d.get('segments_count', 0)} сегментов)"
    ),
    EventType.AUTOCHAT_SEGMENT_SENT: lambda d: (
        f"автодиалог #{d.get('session_id', '?')}: отправлен сегмент "
        f"{d.get('segment_index', '?')}/{d.get('segments_total', '?')}"
    ),
    EventType.AUTOCHAT_SESSION_STOPPED: lambda d: (
        f"автодиалог #{d.get('session_id', '?')} остановлен"
    ),
    EventType.AUTOCHAT_SESSION_ERROR: lambda d: (
        f"автодиалог #{d.get('session_id', '?')}: ошибка — "
        f"{d.get('message', d.get('error', 'неизвестно'))}"
    ),
    EventType.DIALOG_TYPING_OBSERVED: lambda d: (
        f"собеседник печатает (user={d.get('telegram_user_id', '?')})"
    ),
}


def format_message(event: dict[str, Any]) -> str:
    """
    Вернуть готовую русскую фразу для отображения в UI.

    Если шаблона нет — fallback на тип события.
    При статусе error — добавляем причину если она есть в data.error / data.message.
    """
    etype = event.get("type") or "?"
    status = event.get("status") or ""
    data = event.get("data") or {}

    # Системные ошибки — особый случай, обычно text сразу в data
    if etype == EventType.SYSTEM_ERROR:
        msg = data.get("message") or data.get("error") or "системная ошибка"
        return f"системная ошибка: {msg}"

    tpl = _TEMPLATES.get(etype)
    try:
        base = tpl(data) if tpl else etype
    except Exception:
        base = etype

    if status == Status.ERROR:
        err = data.get("error") or data.get("message")
        return f"{base} — ошибка{': ' + str(err) if err else ''}"
    if status == Status.IN_PROGRESS:
        return f"{base} (в процессе)"

    return base
