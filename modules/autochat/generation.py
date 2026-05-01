"""
Сборка контекста для Opus 4.7 и парсинг сегментированного ответа.

ИСТОЧНИК ПРОМТОВ:
  Per-worker `account_prompts.initial_template` / `reply_template` —
  свободный текст оператора. Подставляется в LLM как system-промт с
  заменой плейсхолдеров (см. ниже).

  Файлы `prompts/autochat_*.md` остаются как hardcoded fallback на
  случай битой схемы — в нормальном потоке гейт в session.py /
  service.py не пускает пустой шаблон до этого фолбэка.

ПОДДЕРЖИВАЕМЫЕ ПЛЕЙСХОЛДЕРЫ (общие для initial и reply):
  {current_time}      — текущее UTC-время "DD.MM.YYYY HH:MM:SS"
  {worker_name}       — имя нашего воркера
  {partner_username}  — @username собеседника (без @, может быть пусто)
  {partner_name}      — отображаемое имя собеседника (first + last)
  {partner_bio}       — Telegram-bio собеседника (может быть пусто)

ТОЛЬКО В REPLY (требуют существующего диалога с историей):
  {conversation_history}  — вся история по docs/history_format_spec.md
  {messages_count}        — сколько сообщений в диалоге
  {days_since_first}      — сколько дней с первого сообщения

Незнакомые ключи оставляются как есть — оператор сразу видит опечатку
в превью.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
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

# Fallback — если в БД пусто И гейт обошли. Минимально рабочий промт,
# чтобы система не сломалась. В нормальной работе сюда не попадаем.
_REPLY_FALLBACK = (
    "Ты живой человек в переписке.\n\n"
    "Отвечай короткими сообщениями в тегах <msg>...</msg>. 1–4 сегмента.\n"
    "Без markdown.\n\n"
    "Текущее время: {current_time}"
)
_INITIAL_FALLBACK = (
    "Напиши короткое дружелюбное первое сообщение в Telegram.\n"
    "Одно живое сообщение, без приветствий-шаблонов, без markdown, "
    "без <msg>-тегов. Текущее время: {current_time}"
)


# ─────────────────────────────────────────────────────────────────────
# Подстановка плейсхолдеров
# ─────────────────────────────────────────────────────────────────────

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _strip_comments(text: str) -> str:
    """Убирает <!-- ... --> из промта (для заметок оператора в шаблоне)."""
    return _COMMENT_RE.sub("", text)


def _read_prompt_file(path: Path, fallback: str) -> str:
    """Прочитать файл-фолбэк. Используется только если БД пуста."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("autochat: prompt file not found (%s), using fallback", path)
        return fallback
    except Exception:
        log.exception("autochat: failed to read prompt file %s", path)
        return fallback
    return _strip_comments(raw).strip() + "\n"


def _render(template: str, values: dict[str, str]) -> str:
    """
    Подставить {key} → values[key]. Незнакомый ключ оставляем как есть —
    лучше заметно в превью оператору, чем тихо съедать.
    """
    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key in values:
            return values[key]
        return m.group(0)
    return _PLACEHOLDER_RE.sub(_replace, template)


@dataclass(frozen=True)
class PartnerInfo:
    """Данные собеседника для подстановки в плейсхолдеры."""
    username: str = ""
    name: str = ""
    bio: str = ""

    @classmethod
    def from_dialog_row(cls, row: Any | None) -> "PartnerInfo":
        if row is None:
            return cls()
        name = " ".join(
            p for p in (row["first_name"], row["last_name"]) if p
        ).strip()
        return cls(
            username=(row["username"] or "").lstrip("@"),
            name=name,
            bio=(row["bio"] or "").strip(),
        )

    @classmethod
    def from_resolved_profile(cls, username: str, profile: dict[str, Any]) -> "PartnerInfo":
        """profile — то что отдаёт wrapper.resolve_username()."""
        first = profile.get("first_name") or ""
        last = profile.get("last_name") or ""
        name = " ".join(p for p in (first, last) if p).strip()
        return cls(
            username=(username or "").lstrip("@"),
            name=name,
            bio=(profile.get("bio") or "").strip(),
        )


def _build_placeholders(
    *,
    now: datetime,
    worker_name: str,
    partner: PartnerInfo,
    conversation_history: str | None = None,
    messages_count: int | None = None,
    days_since_first: int | None = None,
) -> dict[str, str]:
    n = now.astimezone(timezone.utc) if now.tzinfo else now
    out: dict[str, str] = {
        "current_time": n.strftime("%d.%m.%Y %H:%M:%S"),
        "worker_name": worker_name or "",
        "partner_username": partner.username,
        "partner_name": partner.name,
        "partner_bio": partner.bio,
    }
    # Reply-only ключи. Всегда добавляем когда заданы — иначе оставляем
    # _render оставлять {key} литералом, оператор увидит опечатку.
    if conversation_history is not None:
        out["conversation_history"] = conversation_history
    if messages_count is not None:
        out["messages_count"] = str(messages_count)
    if days_since_first is not None:
        out["days_since_first"] = str(days_since_first)
    return out


# ─────────────────────────────────────────────────────────────────────
# Загрузка данных из БД для плейсхолдеров
# ─────────────────────────────────────────────────────────────────────

async def _load_worker_name(conn: Any, account_id: int) -> str:
    row = await conn.fetchrow(
        "SELECT name FROM accounts WHERE id = $1", account_id,
    )
    return (row["name"] or "") if row else ""


async def _load_partner_info_for_dialog(
    conn: Any, dialog_id: int,
) -> PartnerInfo:
    row = await conn.fetchrow(
        """
        SELECT username, first_name, last_name, bio
        FROM dialogs WHERE id = $1
        """,
        dialog_id,
    )
    return PartnerInfo.from_dialog_row(row)


async def _load_message_stats(
    conn: Any, dialog_id: int,
) -> tuple[int, int]:
    """Возвращает (messages_count, days_since_first). (0, 0) если сообщений нет."""
    row = await conn.fetchrow(
        """
        SELECT COUNT(*)::int AS cnt, MIN(date) AS first_date
        FROM messages
        WHERE dialog_id = $1 AND deleted_at IS NULL
        """,
        dialog_id,
    )
    if row is None:
        return 0, 0
    cnt = int(row["cnt"] or 0)
    first_date = row["first_date"]
    if first_date is None:
        return 0, 0
    delta = datetime.now(timezone.utc) - first_date
    return cnt, max(0, delta.days)


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
    """Один блок-сообщение по спеке."""
    when = turn.get("date")
    if isinstance(when, datetime):
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
        lines.append("Text: (empty)")
    else:
        if text:
            lines.append(f"Text: {text}")
        lines.extend(attachment_blocks)

    return "\n".join(lines)


def _format_history_text(turns: list[dict[str, Any]]) -> str:
    """История целиком — блоки разделены одной пустой строкой."""
    if not turns:
        return "(история пустая — это начало разговора)"
    return "\n\n".join(_format_message_block(t) for t in turns)


# ─────────────────────────────────────────────────────────────────────
# Загрузка истории из БД
# ─────────────────────────────────────────────────────────────────────

async def _load_turns(conn: Any, dialog_id: int, limit: int) -> list[dict[str, Any]]:
    """
    Тянем последние N non-deleted сообщений + их media. Возвращаем сырые
    поля (text, media) — форматирование делает _format_message_block.

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
    for r in reversed(msg_rows):
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
    account_id: int,
    dialog_id: int,
    now: datetime,
    max_messages: int = CONTEXT_MAX_MESSAGES,
    prompt_override: str | None = None,
) -> list[dict[str, Any]]:
    """
    Собрать messages для chat_completion (reply-режим).

    Источник шаблона:
      • prompt_override (per-worker reply_template из БД) если задан;
      • иначе fallback из файла prompts/autochat_reply_system.md.

    Возвращаемые messages всегда: [system, user:"Ответь сейчас."].
    """
    if prompt_override and prompt_override.strip():
        template = _strip_comments(prompt_override).strip() + "\n"
    else:
        template = _read_prompt_file(REPLY_PROMPT_FILE, _REPLY_FALLBACK)

    worker_name = await _load_worker_name(conn, account_id)
    partner = await _load_partner_info_for_dialog(conn, dialog_id)
    turns = await _load_turns(conn, dialog_id, max_messages)
    history_text = _format_history_text(turns)
    msg_count, days_since = await _load_message_stats(conn, dialog_id)

    placeholders = _build_placeholders(
        now=now,
        worker_name=worker_name,
        partner=partner,
        conversation_history=history_text,
        messages_count=msg_count,
        days_since_first=days_since,
    )
    system_content = _render(template, placeholders)
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "Ответь сейчас."},
    ]


async def render_preview_text(
    conn: Any,
    *,
    template: str,
    account_id: int,
    dialog_id: int | None,
    now: datetime,
    max_messages: int = CONTEXT_MAX_MESSAGES,
) -> str:
    """
    Собрать system-текст для превью на странице редактора промта.

    Не вызывает LLM — просто подставляет реальные значения плейсхолдеров.
    Если `dialog_id` задан — берёт партнёра/историю/статистику из этого
    диалога. Если нет — partner_* пустые, conversation_history с маркером
    «история не выбрана», stats не подставляются (плейсхолдеры останутся
    литералом — оператор увидит что в живом запуске они подставятся).
    """
    template_clean = _strip_comments(template).strip() + "\n" if template.strip() else ""
    if not template_clean:
        return ""

    worker_name = await _load_worker_name(conn, account_id)

    if dialog_id is None:
        placeholders = _build_placeholders(
            now=now,
            worker_name=worker_name,
            partner=PartnerInfo(),
            conversation_history=
                "(история не выбрана — выбери диалог сверху чтобы увидеть с реальными данными)",
        )
    else:
        partner = await _load_partner_info_for_dialog(conn, dialog_id)
        turns = await _load_turns(conn, dialog_id, max_messages)
        history_text = _format_history_text(turns)
        msg_count, days_since = await _load_message_stats(conn, dialog_id)
        placeholders = _build_placeholders(
            now=now,
            worker_name=worker_name,
            partner=partner,
            conversation_history=history_text,
            messages_count=msg_count,
            days_since_first=days_since,
        )

    return _render(template_clean, placeholders)


def build_initial_messages(
    *,
    worker_name: str,
    partner: PartnerInfo,
    now: datetime,
    prompt_override: str | None = None,
) -> list[dict[str, Any]]:
    """
    Messages для первого сообщения.

    История ещё пустая (диалога нет), reply-only плейсхолдеры
    (`conversation_history`/`messages_count`/`days_since_first`) НЕ
    подставляются — если оператор их написал в шаблоне, останутся
    литералом, видно в превью.
    """
    if prompt_override and prompt_override.strip():
        template = _strip_comments(prompt_override).strip() + "\n"
    else:
        template = _read_prompt_file(INITIAL_PROMPT_FILE, _INITIAL_FALLBACK)

    placeholders = _build_placeholders(
        now=now,
        worker_name=worker_name,
        partner=partner,
    )
    system_content = _render(template, placeholders)
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
