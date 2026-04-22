"""
Сборка контекста для Opus 4.7 и парсинг сегментированного ответа.

Промты НЕ захардкожены в коде — берутся из файлов `prompts/*.md`:
  prompts/autochat_reply_system.md    — для ответов в активном диалоге
  prompts/autochat_initial_system.md  — для первого сообщения

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
from datetime import datetime
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


def _has_placeholder(template: str, key: str) -> bool:
    return f"{{{key}}}" in template


# ─────────────────────────────────────────────────────────────────────
# Форматирование истории и медиа
# ─────────────────────────────────────────────────────────────────────

def _media_as_text(media_row: dict[str, Any]) -> str | None:
    """Вложение → текстовая пометка для LLM. None если описать нечего."""
    mtype = media_row.get("type") or ""
    duration = media_row.get("duration")
    transcription = (media_row.get("transcription") or "").strip()
    description = (media_row.get("description") or "").strip()
    t_status = media_row.get("transcription_status") or "none"
    d_status = media_row.get("description_status") or "none"

    def _fail_text(kind: str) -> str:
        return f"[{kind}: не удалось распознать]"

    if mtype in ("voice", "audio", "video_note") and mtype != "video":
        if transcription:
            head = "голос" if mtype in ("voice", "video_note") else "аудио"
            dur = f" {duration}с" if duration else ""
            return f"[{head}{dur}: «{transcription}»]"
        if t_status == "failed":
            return _fail_text("голос" if mtype in ("voice", "video_note") else "аудио")
        if t_status == "pending":
            return "[голос: расшифровывается…]"
        return "[голос: пусто]"

    if mtype == "video":
        parts: list[str] = []
        if description:
            parts.append(f"«{description}»")
        elif d_status == "failed":
            return _fail_text("видео")
        elif d_status == "pending":
            parts.append("описание готовится…")
        if transcription:
            parts.append(f"звук: «{transcription}»")
        elif t_status == "failed":
            parts.append("звук: не удалось")
        dur = f" {duration}с" if duration else ""
        body = "; ".join(parts) if parts else "без описания"
        return f"[видео{dur}: {body}]"

    if mtype in ("photo", "sticker", "gif"):
        label = {"photo": "фото", "sticker": "стикер", "gif": "gif"}[mtype]
        if description:
            return f"[{label}: «{description}»]"
        if d_status == "failed":
            return _fail_text(label)
        if d_status == "pending":
            return f"[{label}: описывается…]"
        return f"[{label}: без описания]"

    if mtype == "document":
        mime = media_row.get("mime_type") or "file"
        if description:
            return f"[документ {mime}: «{description}»]"
        if d_status == "failed":
            return _fail_text("документ")
        return f"[документ {mime}]"

    return None


def _format_message_body(
    text: str | None,
    media_rows: list[dict[str, Any]],
) -> str:
    """Одно сообщение (текст + media) → одна строка для LLM."""
    parts: list[str] = []
    clean_text = (text or "").strip()
    if clean_text:
        parts.append(clean_text)
    for m in media_rows:
        tag = _media_as_text(m)
        if tag:
            parts.append(tag)
    if not parts:
        return "[пустое сообщение]"
    return "\n".join(parts)


def _format_history_text(turns: list[dict[str, Any]]) -> str:
    """
    Превратить историю в plaintext для подстановки в {conversation_history}.

    Формат:
        (2026-04-22 14:23) Ты: привет
        (2026-04-22 14:24) Собеседник: привет, как дела?
        (2026-04-22 14:25) Ты: норм, работаю
    """
    if not turns:
        return "(история пустая — это начало разговора)"
    lines: list[str] = []
    for t in turns:
        when = t["date"]
        if isinstance(when, datetime):
            stamp = when.strftime("%Y-%m-%d %H:%M")
        else:
            stamp = str(when)[:16]
        who = "Ты" if t["is_outgoing"] else "Собеседник"
        body = t["body"]
        lines.append(f"({stamp}) {who}: {body}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Загрузка истории из БД (общая для обоих режимов)
# ─────────────────────────────────────────────────────────────────────

async def _load_turns(conn: Any, dialog_id: int, limit: int) -> list[dict[str, Any]]:
    """
    Тянем последние N non-deleted сообщений + их media, формируем
    список {is_outgoing, date, body}. Хронологический порядок (старые→новые).
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
            "body": _format_message_body(r["text"], media_by_msg.get(r["id"], [])),
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
) -> list[dict[str, Any]]:
    """
    Собрать messages для chat_completion.

    Поведение зависит от файла prompts/autochat_reply_system.md:
      • Если в нём есть `{conversation_history}` — история подставляется
        текстом в system-промт, в messages идёт только [system, user:"..."]
        с нейтральной просьбой ответить.
      • Иначе — стандарт: [system, user, assistant, user, ...] через роли.
    """
    template = _read_prompt_file(REPLY_PROMPT_FILE, _REPLY_FALLBACK)
    current_time = now.strftime("%Y-%m-%d %H:%M")

    turns = await _load_turns(conn, dialog_id, max_messages)

    if _has_placeholder(template, "conversation_history"):
        # Режим A: всё в system, роли не используем
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

    # Режим B: роль-based history
    system_content = _render(template, {
        "current_time": current_time,
        "user_system_prompt": (system_prompt or "").strip(),
    })
    out: list[dict[str, Any]] = [
        {"role": "system", "content": system_content},
    ]
    for t in turns:
        role = "assistant" if t["is_outgoing"] else "user"
        out.append({"role": role, "content": t["body"]})
    if not turns:
        # Диалога ещё нет — пусть LLM что-то скажет.
        out.append({"role": "user", "content": "Поздоровайся или продолжи разговор."})
    return out


def build_initial_messages(
    *,
    system_prompt: str,
    initial_prompt: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """
    Messages для первого сообщения. Файл — prompts/autochat_initial_system.md.
    Все три плейсхолдера: current_time, user_system_prompt, user_initial_prompt.
    """
    template = _read_prompt_file(INITIAL_PROMPT_FILE, _INITIAL_FALLBACK)
    system_content = _render(template, {
        "current_time": now.strftime("%Y-%m-%d %H:%M"),
        "user_system_prompt": (system_prompt or "").strip(),
        "user_initial_prompt": (initial_prompt or "").strip() or "Напиши дружелюбное первое сообщение.",
    })
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "Напиши первое сообщение."},
    ]


# ─────────────────────────────────────────────────────────────────────
# Парсер сегментов
# ─────────────────────────────────────────────────────────────────────

_MSG_TAG_RE = re.compile(r"<msg>(.*?)</msg>", re.DOTALL | re.IGNORECASE)


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
