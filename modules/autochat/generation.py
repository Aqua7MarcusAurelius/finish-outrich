"""
Сборка контекста для Opus 4.7 и парсинг сегментированного ответа.

Никуда в шину не пишет, с БД работает только на чтение — чистые функции
плюс один async-хелпер с conn.

См. docs/autochat.md → раздел "Генерация ответа".
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

# Максимальное количество сообщений диалога, которые тянем в контекст.
# При типичной переписке этого более чем достаточно; чинит потенциальный
# "LLM-захлёб" на очень старых диалогах.
CONTEXT_MAX_MESSAGES = 200

# Максимальное число сегментов в одном ответе — защита от взбесившейся
# модели, которая нагенерит 50 <msg>-тегов.
MAX_SEGMENTS = 8

# Жёсткий лимит длины одного сегмента (символов). Больше — усечём.
MAX_SEGMENT_LEN = 2000

SEGMENTATION_INSTRUCTION = """\
Отвечай короткими сообщениями, как в живом чате.
Разделяй ответ на 1–4 отдельных сообщения. Каждое сообщение оборачивай
в теги <msg>...</msg>.

Пример:
<msg>привет</msg><msg>да, я как раз об этом думал</msg>

Правила:
- Не нумеруй сообщения.
- Не используй markdown (жирный/курсив/списки). Обычный текст.
- Каждое сообщение — одна мысль или короткая фраза.
- Часто достаточно 1–2 сегментов; больше 4 — только если правда много мыслей.
- Если отвечать не на что — верни один короткий живой <msg>."""


# ─────────────────────────────────────────────────────────────────────
# Формирование содержимого user/assistant-сообщения из БД-строки
# ─────────────────────────────────────────────────────────────────────

def _media_as_text(media_row: dict[str, Any]) -> str | None:
    """
    Превращаем вложение в текстовую пометку для LLM.
    Возвращаем None если описать нечего (нет типа/данных).
    """
    mtype = media_row.get("type") or ""
    duration = media_row.get("duration")
    transcription = (media_row.get("transcription") or "").strip()
    description = (media_row.get("description") or "").strip()
    t_status = media_row.get("transcription_status") or "none"
    d_status = media_row.get("description_status") or "none"

    def _fail_text(kind: str) -> str:
        return f"[{kind}: не удалось распознать]"

    if mtype in ("voice", "audio", "video_note") and mtype != "video":
        # Голосовые / аудио / кружок — показываем текст транскрипции.
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
        # Видео — и транскрипция, и описание (оба ценны).
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


def _format_message_content(
    text: str | None,
    media_rows: list[dict[str, Any]],
) -> str:
    """
    Превращаем сообщение (текст + media) в одну строку для LLM.
    Пустые сообщения оставляем как "[пустое сообщение]" — чтобы
    сохранился факт "кто-то написал", даже если внутри ничего.
    """
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


# ─────────────────────────────────────────────────────────────────────
# Сборка контекста из БД
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
    Достать переписку с dialog_id и собрать messages для chat_completion.

    System-промт получает:
      - user-текст (то что задал пользователь в /autochat/start);
      - инструкцию сегментации;
      - текущее время (чтобы LLM могла учитывать часть суток).

    История:
      - последние N сообщений, отсортированных хронологически ASC;
      - soft-deleted (deleted_at IS NOT NULL) пропускаются;
      - медиа склеиваются в текст с транскриптами/описаниями.
    """
    # Берём N самых свежих (DESC LIMIT), затем реверсим в хронологию.
    msg_rows = await conn.fetch(
        """
        SELECT id, is_outgoing, date, text
        FROM messages
        WHERE dialog_id = $1 AND deleted_at IS NULL
        ORDER BY date DESC, id DESC
        LIMIT $2
        """,
        dialog_id, max_messages,
    )

    # asyncpg возвращает Record — конвертируем в обычные dict для удобства
    messages: list[dict[str, Any]] = [dict(r) for r in msg_rows]
    messages.reverse()  # хронологически: старые → новые

    if not messages:
        # Диалога ещё не существует или пусто — возвращаем только system.
        return [{"role": "system", "content": _system_content(system_prompt, now)}]

    # Пачкой тащим все media по message_id'ам
    msg_ids = [m["id"] for m in messages]
    media_rows = await conn.fetch(
        "SELECT * FROM media WHERE message_id = ANY($1::int[]) ORDER BY id",
        msg_ids,
    )
    media_by_msg: dict[int, list[dict[str, Any]]] = {}
    for r in media_rows:
        media_by_msg.setdefault(r["message_id"], []).append(dict(r))

    out: list[dict[str, Any]] = [
        {"role": "system", "content": _system_content(system_prompt, now)},
    ]

    for m in messages:
        role = "assistant" if m["is_outgoing"] else "user"
        content = _format_message_content(m["text"], media_by_msg.get(m["id"], []))
        out.append({"role": role, "content": content})

    return out


def _system_content(system_prompt: str, now: datetime) -> str:
    clean = (system_prompt or "").strip()
    time_line = f"Текущее время: {now.strftime('%Y-%m-%d %H:%M')} (UTC)."
    return f"{clean}\n\n{time_line}\n\n{SEGMENTATION_INSTRUCTION}"


def build_initial_messages(
    *,
    system_prompt: str,
    initial_prompt: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """
    Messages для генерации первого сообщения автосессии.

    Отличия от обычной генерации:
      - Истории нет (диалог ещё не начался).
      - Просим одно сообщение (не набор сегментов) — первое обращение
        к незнакомому человеку не должно быть стеной из 4 сообщений.
    """
    clean_system = (system_prompt or "").strip()
    time_line = f"Текущее время: {now.strftime('%Y-%m-%d %H:%M')} (UTC)."
    system_content = (
        f"{clean_system}\n\n{time_line}\n\n"
        "Сейчас ты пишешь первое сообщение незнакомому человеку. "
        "Отвечай одним живым коротким сообщением — без markdown, без тегов, "
        "без приветствий-шаблонов. Просто первая естественная фраза."
    )
    user_content = (initial_prompt or "").strip() or (
        "Придумай дружелюбное первое сообщение."
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


# ─────────────────────────────────────────────────────────────────────
# Парсер сегментов
# ─────────────────────────────────────────────────────────────────────

_MSG_TAG_RE = re.compile(r"<msg>(.*?)</msg>", re.DOTALL | re.IGNORECASE)


def parse_segments(response: str) -> list[str]:
    """
    Разбить ответ Opus на отдельные сообщения по тегам <msg>.

    Если тегов нет (модель проигнорировала формат) — возвращаем весь
    ответ как единственный сегмент (fallback). Пустые сегменты
    (после strip) отбрасываем. Длинные сегменты режем по MAX_SEGMENT_LEN.
    """
    if response is None:
        return []
    text = response.strip()
    if not text:
        return []

    matches = [m.group(1).strip() for m in _MSG_TAG_RE.finditer(text)]
    segments = [s for s in matches if s]

    if not segments:
        # Fallback — единый сегмент. Убираем потенциально затёсанные
        # "голые" теги если модель их всё же вставила.
        cleaned = _MSG_TAG_RE.sub("", text).strip()
        if cleaned:
            segments = [cleaned]

    # Safety
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
    Для первого сообщения: если модель всё равно вставила <msg>-теги,
    снимаем их и склеиваем. Возвращаем одну чистую строку.
    """
    if not response:
        return ""
    segments = parse_segments(response)
    if not segments:
        return response.strip()
    # Склеиваем первое сообщение через пробел — для короткой реплики
    # обычно получится один сегмент и склеивание не сработает.
    # Отдельной строки с newline не даём, чтобы в Telegram ушло
    # одним визуальным сообщением.
    return " ".join(segments).strip()
