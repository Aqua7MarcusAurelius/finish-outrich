"""
Per-worker промты для AutoChat — один текст на каждый из двух режимов.

Источник правды: таблица `account_prompts` (одна строка на воркера),
поля `initial_template` и `reply_template`. Оператор редактирует на
странице `/workers/{id}/prompt`; превью без вызова LLM — через эндпоинт
`POST /accounts/{id}/prompts/preview`.

Подстановка плейсхолдеров делается в `modules/autochat/generation.py`:
оператор пишет `{partner_name}`, `{current_time}`, `{conversation_history}`
и т.п. — система подставляет значения при генерации. Незнакомые ключи
оставляются как есть (видно в превью что опечатался).

Гейты:
  • пустой `initial_template` → блок `/autochat/start`
  • пустой `reply_template` → `autochat.generation_skipped` без вызова LLM
"""
from __future__ import annotations

from dataclasses import dataclass

from core import db


@dataclass(frozen=True)
class WorkerPrompts:
    initial_template: str = ""
    reply_template: str = ""

    def has_initial(self) -> bool:
        return bool(self.initial_template and self.initial_template.strip())

    def has_reply(self) -> bool:
        return bool(self.reply_template and self.reply_template.strip())


_EMPTY = WorkerPrompts()


async def load_for_account(account_id: int) -> WorkerPrompts:
    """Читает строку account_prompts. Если её нет — оба поля пустые."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT initial_template, reply_template FROM account_prompts WHERE account_id = $1",
            account_id,
        )
    if row is None:
        return _EMPTY
    return WorkerPrompts(
        initial_template=row["initial_template"] or "",
        reply_template=row["reply_template"] or "",
    )
