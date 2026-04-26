"""
Сборка контекста для Opus 4.7 и парсинг сегментированного ответа.

ИСТОЧНИК ПРОМТОВ:
  • основной — таблица `account_prompts` (per-worker, см. prompts.py).
    Передаётся в build_*() через параметр `prompt_override`.
  • fallback — файлы `prompts/autochat_*.md` (только если override пустой;
    используется как последний рубеж для совместимости/тестов, не для
    продакшна — пустой override блокируется ВЫЩЕ, в session.py / service.py).

Файлы читаются при каждой генерации — правишь файл, следующий запрос
использует новый текст без рестарта.

Поддерживаемые плейсхолдеры:
  {current_time}          — текущее UTC-время "YYYY-MM-DD HH:MM"
  {user_system_prompt}    — из /autochat/start (или autochat_sessions.system_prompt)
  {user_initial_prompt}   — только в initial-промте
  {conversation_history}  — (опционально) вся история текстом. Если плейсхолдер
                            есть — история ПЕРЕСТАЁТ идти role-messages в LLM,
                            а подставляется текстом в system.

См. docs/autochat.md → раздел "Редактирование промтов".
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Параметры
# ─────────────────────────────────────────────────────────────────────

CONTEXT_MAX_MESSAGES = 200
MAX_SEGMENTS = 8
MAX_SEGMENT_LEN = 2000

# Папка prompts/ в корне проекта
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = _PROJECT_ROOT / "prompts"

REPLY_PROMPT_FILE = PROMPTS_DIR / "autochat_reply_system.md"
INITIAL_PROMPT_FILE = PROMPTS_DIR / "autochat_initial_system.md"

# Fallback — если файла нет или не читается. Минимальный рабочий промт,
# система не сломается, но пользователь увидит скучный дефолт.
_REPLY_FALLBACK = (
    "Ты живой человек в переписке. {user_system_prompt}\n\n"
    "Отвечай короткими сообщениями в тегах <msg>...</msg>. 1–4 сегмента.\n"
    "Без markdown.\n\n"
    "Текущее время: {current_time}"
)
_INITIAL_FALLBACK = (
    "Напиши короткое дружелюбное первое сообщение в Telegram.\n\n"
    "{user_system_prompt}\n\n"
    "Задача: {user_initial_prompt}\n\n"
    "Одно живое сообщение, без приветствий-шаблонов, без markdown, "
    "без <msg>-тегов. Текущее время: {current_time}"
)


# ─────────────────────────────────────────────────────────────────────
# Работа с файлами промтов
# ─────────────────────────────────────────────────────────────────────

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _strip_comments(text: str) -> str:
    """Убирает <!-- ... --> комментарии из промта."""
    return _COMMENT_RE.sub("", text)


def _read_prompt_file(path: Path, fallback: str) -> str:
    """Прочитать файл промта. При отсутствии / ошибке — fallback + warning."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning(
            "autochat: prompt file not found (%s), using fallback", path,
        )
        return fallback
    except Exception:
        log.exception("autochat: failed to read prompt file %s", path)
        return fallback
    return _strip_comments(raw).strip() + "\n"


def _render(template: str, values: dict[str, str]) -> str:
    """
    Подставить {key} → values[key]. Если ключа нет в values — оставляем
    {key} как есть (лучше заметно в UI, чем молча съедать).
    """
    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key in values:
            return values[key]
        return m.group(0)  # оставляем как есть
    return _PLACEHOLDER_RE.sub(_replace, template)


# ─────────────────────────────────────────────────────────────────────
# Форматирование истории — спека: docs/history_format_spec.md
# ─────────────────────────────────────────────────────────────────────

# Подписи блоков по типу media.type. Типы вне таблицы пропускаются.
_MEDIA_LABELS: dict[str, str] = {
    "voice":      "Voice",
    "audio":      "Audio",
    "video_note": "VideoNote",
    "video":      "Video",
    "photo":      "Photo",
    "sticker":    "Sticker",
    "gif":        "GIF",
    "document":   "Document",
}

# День недели по datetime.weekday(): 0=Monday … 6=Sunday.
_RU_WEEKDAYS = (
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
)


def _value_or_failed(text: str | None, status: str | None) -> str | None:
    """
    Подполе вложения — что печатать.

    • Есть непустой текст → печатаем его.
    • Нет текста, статус failed → "(failed)".
    • Иначе → None (подполе вообще не печатается;
      "pending" в истории не бывает, см. правило 5 спеки).
    """
    if text and text.strip():
        return text.strip()
    if status == "failed":
        return "(failed)"
    return None


def _format_attachment_block(
    media_row: dict[str, Any], label: str, index_in_type: int,
) -> str | None:
    """
    Один блок вложения с подполями (с двухпробельным отступом).
    None — если ни одно подполе не должно печататься
    (см. правило 10 в спеке: пустое вложение пропускается целиком).
    """
    mtype = media_row.get("type") or ""
    sub_lines: list[str] = []

    # Description применима к: video, video_note, photo, sticker, gif, document.
    if mtype in ("video", "video_note", "photo", "sticker", "gif", "document"):
        d = _value_or_failed(
            media_row.get("description"),
            media_row.get("description_status"),
        )
        if d is not None:
            sub_lines.append(f"  Description: {d}")

    # Transcription применима к: voice, audio, video_note, video.
    if mtype in ("voice", "audio", "video_note", "video"):
        t = _value_or_failed(
            media_row.get("transcription"),
            media_row.get("transcription_status"),
        )
        if t is not None:
            sub_lines.append(f"  Transcription: {t}")

    if not sub_lines:
        return None
    return f"{label} {index_in_type}:\n" + "\n".join(sub_lines)


def _format_message_block(turn: dict[str, Any]) -> str:
    """
    Один блок-сообщение по спеке. Многострочный, без хвостовой пустой строки —
    разделители между блоками добавляет _format_history_text.
    """
    when = turn.get("date")
    if isinstance(when, datetime):
        # Все даты в БД — TIMESTAMPTZ, приходят tz-aware. Приводим к UTC.
        n = when.astimezone(timezone.utc) if when.tzinfo else when
        date_s = n.strftime("%d.%m.%Y")
        time_s = n.strftime("%H:%M:%S")
        weekday_s = _RU_WEEKDAYS[n.weekday()]
    else:
        date_s = "—"
        time_s = "—"
        weekday_s = "—"

    author = "Ты" if turn.get("is_outgoing") else "Собеседник"

    lines: list[str] = [
        f"Author: {author}",
        f"Date: {date_s}",
        f"Weekday: {weekday_s}",
        f"Time: {time_s}",
    ]

    text = (turn.get("text") or "").strip()
    media_rows = turn.get("media") or []

    # Считаем индексы внутри типа отдельно (правило 9): два фото и видео =
    # Photo 1, Photo 2, Video 1. Незнакомые типы пропускаем.
    type_counters: dict[str, int] = {}
    attachment_blocks: list[str] = []
    for m in media_rows:
        label = _MEDIA_LABELS.get(m.get("type") or "")
        if label is None:
            continue
        type_counters[label] = type_counters.get(label, 0) + 1
        block = _format_attachment_block(m, label, type_counters[label])
        if block is not None:
            attachment_blocks.append(block)

    if not text and not attachment_blocks:
        # Правило: ни текста, ни usable вложений → Text: (empty), не пропускать.
        lines.append("Text: (empty)")
    else:
        if text:
            lines.append(f"Text: {text}")
        lines.extend(attachment_blocks)

    return "\n".join(lines)


def _format_history_text(turns: list[dict[str, Any]]) -> str:
    """
    История целиком — блоки разделены одной пустой строкой.
    """
    if not turns:
        return "(история пустая — это начало разговора)"
    return "\n\n".join(_format_message_block(t) for t in turns)


# ─────────────────────────────────────────────────────────────────────
# Загрузка истории из БД
# ─────────────────────────────────────────────────────────────────────

async def _load_turns(conn: Any, dialog_id: int, limit: int) -> list[dict[str, Any]]:
    """
    Тянем последние N non-deleted сообщений + их media. Возвращаем сырые
    поля (text, media) — форматирование делает _format_message_block по
    спеке docs/history_format_spec.md.

    Хронологический порядок: старые → новые.
    """
    msg_rows = await conn.fetch(
        """
        SELECT id, is_outgoing, date, text
        FROM messages
        WHERE dialog_id = $1 AND deleted_at IS NULL
        ORDER BY date DESC, id DESC
        LIMIT $2
        """,
        dialog_id, limit,
    )
    if not msg_rows:
        return []

    msg_ids = [r["id"] for r in msg_rows]
    media_rows = await conn.fetch(
        "SELECT * FROM media WHERE message_id = ANY($1::int[]) ORDER BY id",
        msg_ids,
    )
    media_by_msg: dict[int, list[dict[str, Any]]] = {}
    for r in media_rows:
        media_by_msg.setdefault(r["message_id"], []).append(dict(r))

    turns: list[dict[str, Any]] = []
    for r in reversed(msg_rows):  # старые → новые
        turns.append({
            "is_outgoing": bool(r["is_outgoing"]),
            "date": r["date"],
            "text": r["text"],
            "media": media_by_msg.get(r["id"], []),
        })
    return turns


# ─────────────────────────────────────────────────────────────────────
# Сборка messages для LLM
# ─────────────────────────────────────────────────────────────────────

async def build_conversation_context(
    conn: Any,
    *,
    dialog_id: int,
    system_prompt: str,
    now: datetime,
    max_messages: int = CONTEXT_MAX_MESSAGES,
    prompt_override: str | None = None,
) -> list[dict[str, Any]]:
    """
    Собрать messages для chat_completion.

    Источник шаблона:
      • prompt_override (per-worker rendered template из prompts.py) если
        задан и не пустой;
      • иначе fallback из файла prompts/autochat_reply_system.md.

    Историю всегда подставляем текстом в system-промт по плейсхолдеру
    {conversation_history} в формате docs/history_format_spec.md.
    Если в шаблоне нет этого плейсхолдера (legacy fallback) — история не
    попадёт в промт; в нормальном потоке render_reply_system() всегда
    добавляет секцию "# История переписки" с плейсхолдером.

    Возвращаемые messages всегда: [system, user:"Ответь сейчас."].
    """
    if prompt_override and prompt_override.strip():
        template = prompt_override.strip() + "\n"
    else:
        template = _read_prompt_file(REPLY_PROMPT_FILE, _REPLY_FALLBACK)

    # current_time — единый формат с историей (DD.MM.YYYY HH:MM:SS UTC).
    n = now.astimezone(timezone.utc) if now.tzinfo else now
    current_time = n.strftime("%d.%m.%Y %H:%M:%S")

    turns = await _load_turns(conn, dialog_id, max_messages)
    history_text = _format_history_text(turns)

    system_content = _render(template, {
        "current_time": current_time,
        "user_system_prompt": (system_prompt or "").strip(),
        "conversation_history": history_text,
    })
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "Ответь сейчас."},
    ]


def build_initial_messages(
    *,
    now: datetime,
    prompt_override: str | None = None,
) -> list[dict[str, Any]]:
    """
    Messages для первого сообщения.

    Источник шаблона:
      • prompt_override (per-worker initial_system из БД) если задан;
      • иначе fallback из файла prompts/autochat_initial_system.md.

    Активный плейсхолдер: {current_time} (DD.MM.YYYY HH:MM:SS UTC).
    Устаревшие {user_system_prompt} и {user_initial_prompt} (если оператор
    их зачем-то оставил в шаблоне) тихо подставляются пустой строкой —
    per-session inputs убраны вместе с упрощением "+ Новый авто-диалог".
    """
    if prompt_override and prompt_override.strip():
        template = _strip_comments(prompt_override).strip() + "\n"
    else:
        template = _read_prompt_file(INITIAL_PROMPT_FILE, _INITIAL_FALLBACK)
    n = now.astimezone(timezone.utc) if now.tzinfo else now
    system_content = _render(template, {
        "current_time": n.strftime("%d.%m.%Y %H:%M:%S"),
        "user_system_prompt": "",
        "user_initial_prompt": "",
    })
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "Напиши первое сообщение."},
    ]


# ─────────────────────────────────────────────────────────────────────
# Парсер сегментов
# ─────────────────────────────────────────────────────────────────────

_MSG_TAG_RE = re.compile(r"<msg>(.*?)</msg>", re.DOTALL | re.IGNORECASE)

# Маркер завершения диалога. LLM ставит его когда поняла что все цели
# разговора достигнуты. Принимаем варианты `<finishdialog/>`,
# `<finishdialog>`, `< finishdialog />` (на случай если модель добавит
# пробелы). Регистр игнорируется.
_FINISH_MARKER_RE = re.compile(r"<\s*finishdialog\s*/?\s*>", re.IGNORECASE)


def extract_finish_marker(response: str) -> tuple[str, bool]:
    """
    Ищет маркер <finishdialog/> в ответе LLM.

    Возвращает (text_без_маркера, found). Все вхождения маркера
    вырезаются — он чисто служебный и не должен попасть ни в один
    отправляемый сегмент.
    """
    if not response:
        return response or "", False
    found = bool(_FINISH_MARKER_RE.search(response))
    cleaned = _FINISH_MARKER_RE.sub("", response)
    return cleaned, found


def parse_segments(response: str) -> list[str]:
    """
    Разобрать ответ Opus на сегменты по <msg>-тегам.
    Fallback: если тегов нет — весь ответ = один сегмент.
    """
    if response is None:
        return []
    text = response.strip()
    if not text:
        return []

    matches = [m.group(1).strip() for m in _MSG_TAG_RE.finditer(text)]
    segments = [s for s in matches if s]

    if not segments:
        cleaned = _MSG_TAG_RE.sub("", text).strip()
        if cleaned:
            segments = [cleaned]

    if len(segments) > MAX_SEGMENTS:
        log.warning(
            "autochat: got %d segments, trimming to %d",
            len(segments), MAX_SEGMENTS,
        )
        segments = segments[:MAX_SEGMENTS]

    out: list[str] = []
    for s in segments:
        if len(s) > MAX_SEGMENT_LEN:
            s = s[:MAX_SEGMENT_LEN].rstrip()
        if s:
            out.append(s)
    return out


def sanitize_initial_response(response: str) -> str:
    """
    Для первого сообщения: если модель всё-таки вставила <msg>-теги,
    снимаем и склеиваем. Одно сообщение, один visual bubble в Telegram.
    """
    if not response:
        return ""
    segments = parse_segments(response)
    if not segments:
        return response.strip()
    return " ".join(segments).strip()
