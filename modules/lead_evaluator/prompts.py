"""Загрузка промта оценщика и подстановка плейсхолдеров.

Поддерживаемые плейсхолдеры — те же что в autochat-промтах плюс
`{partner_gender}` (см. prompts/lead_evaluator_system.md).

Источник промта — двухуровневый (2026-07-20):
  1. Per-account: `account_prompts.evaluator_template` — если непустой,
     используется он. Даёт разным семействам воркеров свой оценщик с
     разными критериями «успеха» (другой поставщик лидов = другие задачи).
  2. Глобальный fallback: файл `prompts/lead_evaluator_system.md`
     (или встроенный `_FALLBACK`, если файла нет). Так работали все
     воркеры до появления per-account поля — обратная совместимость.

Модель, парсер вердикта и словарь вердиктов (`success`/`archive`) от
источника промта НЕ зависят — переопределяется только текст-критерии.

HTML-комментарии `<!-- ... -->` вырезаются перед отправкой в LLM — их
можно использовать как заметки для редактора, в промт они не попадут.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules.autochat.generation import (
    PartnerInfo,
    _format_history_text,
    _load_message_stats,
    _load_partner_info_for_dialog,
    _load_turns,
    _load_worker_name,
)

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_FILE = _PROJECT_ROOT / "prompts" / "lead_evaluator_system.md"

# Fallback на случай если файла нет — минимально рабочий промт.
_FALLBACK = (
    "Ты оцениваешь лида по переписке.\n"
    "Если лид реальный, заинтересованный и подходящий — выведи "
    "<verdict>success</verdict>, иначе <verdict>archive</verdict>.\n"
    "Извлеки если есть: <name>, <age>, <city>, <workplace>, <interests>.\n\n"
    "Контекст: {current_time}\n"
    "Лид: @{partner_username}\n"
    "Переписка:\n{conversation_history}\n"
)

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def _strip_comments(template: str) -> str:
    return _COMMENT_RE.sub("", template)


def _render(template: str, values: dict[str, str]) -> str:
    """Подставить {key} → values[key]. Незнакомые ключи оставить как есть."""
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return values[key]
        return match.group(0)
    return _PLACEHOLDER_RE.sub(repl, template)


def _now_str(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M:%S")


def _read_prompt_file() -> str:
    try:
        text = PROMPT_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning(
            "lead_evaluator: prompt file not found (%s), using fallback",
            PROMPT_FILE,
        )
        return _FALLBACK
    except Exception:
        log.exception(
            "lead_evaluator: failed to read prompt file, using fallback",
        )
        return _FALLBACK
    return text


async def _load_account_evaluator_template(conn: Any, account_id: int) -> str:
    """Per-account промт оценщика из `account_prompts.evaluator_template`.

    Возвращает уже стрипнутую строку; '' если строки в таблице нет либо
    поле пустое (оба случая означают «используй глобальный по умолчанию»).
    """
    row = await conn.fetchval(
        "SELECT evaluator_template FROM account_prompts WHERE account_id = $1",
        account_id,
    )
    return (row or "").strip()


def resolve_evaluator_template(account_template: str, global_template: str) -> str:
    """Выбор промта: непустой per-account побеждает, иначе глобальный.

    Чистая функция (без БД/файлов) — вся логика приоритета здесь, чтобы
    её можно было проверить smoke-тестом без Postgres.
    """
    if account_template and account_template.strip():
        return account_template
    return global_template


def _build_placeholder_values(
    *,
    now: datetime,
    worker_name: str,
    partner: PartnerInfo,
    gender: str | None,
    conversation_history: str,
    msg_count: int,
    days_since_first: int,
) -> dict[str, str]:
    """Собрать словарь {ключ: строка} для подстановки в шаблон."""
    return {
        "current_time": _now_str(now),
        "worker_name": worker_name or "",
        "partner_username": partner.username or "",
        "partner_name": (f"{partner.first_name or ''} "
                         f"{partner.last_name or ''}").strip(),
        "partner_first_name": partner.first_name or "",
        "partner_last_name": partner.last_name or "",
        "partner_bio": partner.bio or "",
        "partner_birthday": partner.birthday or "",
        "partner_gender": gender or "",
        "conversation_history": conversation_history,
        "messages_count": str(msg_count),
        "days_since_first": str(days_since_first),
    }


async def build_evaluator_messages(
    conn: Any,
    *,
    account_id: int,
    dialog_id: int,
    gender: str | None,
    now: datetime,
    max_messages: int = 200,
) -> list[dict[str, str]]:
    """
    Собрать [{system: ...}, {user: "Оцени."}] для LLM.

    Источник системного промта: per-account `evaluator_template` если он
    непустой, иначе глобальный файл (см. модуль-докстринг). Все плейсхолдеры
    в промте будут подставлены реальными значениями из БД. Если плейсхолдера
    в промте нет — соответствующий запрос в БД тоже делается (это копеечно и
    проще чем парсить и условно дёргать), он просто не использован в
    финальной строке.
    """
    account_template = await _load_account_evaluator_template(conn, account_id)
    template_raw = resolve_evaluator_template(account_template, _read_prompt_file())
    template = _strip_comments(template_raw).strip() + "\n"

    worker_name = await _load_worker_name(conn, account_id)
    partner = await _load_partner_info_for_dialog(conn, dialog_id)
    turns = await _load_turns(conn, dialog_id, max_messages)
    history_text = _format_history_text(turns)
    msg_count, days_since = await _load_message_stats(conn, dialog_id)

    values = _build_placeholder_values(
        now=now,
        worker_name=worker_name,
        partner=partner,
        gender=gender,
        conversation_history=history_text,
        msg_count=msg_count,
        days_since_first=days_since,
    )
    system_content = _render(template, values)
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "Оцени лида по этой переписке."},
    ]
