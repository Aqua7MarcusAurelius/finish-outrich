"""
Per-worker промты для AutoChat — структурированный конфиг.

Источник правды: таблица `account_prompts` (одна строка на воркера).
Поля для reply разбиты на 8 семантических секций — `render_reply_system()`
собирает из них итоговый system-текст с заголовками. Пустые секции
пропускаются.

initial_system пока остаётся одним текстом (initial вне скоупа этой
переделки).

Файлы `prompts/autochat_*.md` остаются как hardcoded fallback внутри
generation.py — нормальный поток до них не доходит, гейт в session.py /
service.py блокирует пустоту.
"""
from __future__ import annotations

from dataclasses import dataclass

from core import db


# ─────────────────────────────────────────────────────────────────────
# Дефолты для критичных секций. Подставляются ТОЛЬКО при первом открытии
# редактора (когда строки в БД ещё нет) — чтобы оператор не забыл про
# `<msg>`-теги и базовые запреты. Сохранение → персистится в БД, дальше
# дефолты не накладываются.
# ─────────────────────────────────────────────────────────────────────

DEFAULT_FORBIDDEN = (
    "- Не раскрывай что ты ИИ / бот / GPT / Claude / любая программа.\n"
    "- Не используй markdown (жирный, курсив, списки, заголовки).\n"
    "- Не задавай по 5 вопросов подряд — максимум один.\n"
    "- Не пиши длинные монологи. Если тема сложная — разбей на 2–3 коротких сообщения.\n"
    "- Политику и религию — не поднимай первой. Если спросили — отвечай уклончиво."
)

DEFAULT_FORMAT_REPLY = (
    "Разделяй ответ на 1–4 отдельных сообщения. Каждое оборачивай в теги <msg>...</msg>.\n\n"
    "Пример: <msg>привет</msg><msg>да, я как раз об этом думала</msg>\n\n"
    "Правила:\n"
    "- Не нумеруй сообщения.\n"
    "- Каждое — одна мысль или короткая фраза.\n"
    "- Часто достаточно 1–2 сегментов. 4 — только если правда много мыслей.\n"
    "- НЕ копируй в свои ответы служебные метки времени вида \"(отправлено 2026-04-24 10:13)\" — "
    "это для тебя, не часть формата ответа."
)


# Порядок и заголовки секций в собранном промте. Контракт между UI и LLM —
# не меняется без согласования (LLM привыкает к структуре, переименование
# заголовков снижает качество ответов).
_REPLY_SECTIONS: list[tuple[str, str]] = [
    ("fabula",       "# Контекст задачи"),
    ("bio",          "# Кто ты"),
    ("style",        "# Как ты общаешься"),
    ("forbidden",    "# Чего НЕ делать"),
    ("length_hint",  "# Темп разговора"),
    ("goals",        "# Цели"),
    ("format_reply", "# Формат ответа"),
    ("examples",     "# Примеры удачных ответов"),
]


@dataclass(frozen=True)
class WorkerPrompts:
    fabula: str = ""
    bio: str = ""
    style: str = ""
    forbidden: str = ""
    length_hint: str = ""
    goals: str = ""
    format_reply: str = ""
    examples: str = ""
    initial_system: str = ""

    def has_any_reply_field(self) -> bool:
        """True если хоть одна reply-секция непустая (после strip)."""
        for key, _ in _REPLY_SECTIONS:
            value = getattr(self, key)
            if value and value.strip():
                return True
        return False

    def has_initial(self) -> bool:
        return bool(self.initial_system and self.initial_system.strip())


_EMPTY = WorkerPrompts()


async def load_for_account(account_id: int) -> WorkerPrompts:
    """Читает строку account_prompts. Если её нет — все поля пустые."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT fabula, bio, style, forbidden, length_hint, goals,
                   format_reply, examples, initial_system
            FROM account_prompts WHERE account_id = $1
            """,
            account_id,
        )
    if row is None:
        return _EMPTY
    return WorkerPrompts(
        fabula=row["fabula"] or "",
        bio=row["bio"] or "",
        style=row["style"] or "",
        forbidden=row["forbidden"] or "",
        length_hint=row["length_hint"] or "",
        goals=row["goals"] or "",
        format_reply=row["format_reply"] or "",
        examples=row["examples"] or "",
        initial_system=row["initial_system"] or "",
    )


def render_reply_system(prompts: WorkerPrompts) -> str:
    """
    Собрать system-промт для reply-генерации из непустых полей.

    Возвращает шаблон с подставленными секциями + неподставленными
    плейсхолдерами {current_time} и {user_system_prompt} в финальной
    "# Контекст" секции — generation.py подставит их при сборке messages.
    """
    parts: list[str] = []
    for key, header in _REPLY_SECTIONS:
        value = (getattr(prompts, key) or "").strip()
        if not value:
            continue
        parts.append(f"{header}\n\n{value}")

    # Хвост: динамический контекст + история переписки. Заголовки
    # добавляем всегда — generation.py подставит плейсхолдеры.
    parts.append(
        "# Контекст\n\n"
        "Текущее время: {current_time}\n\n"
        "Заметка про собеседника: {user_system_prompt}"
    )
    parts.append(
        "# История переписки\n\n"
        "{conversation_history}"
    )
    return "\n\n".join(parts)
