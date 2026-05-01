"""account_prompts → одно поле под весь промт + плейсхолдеры

Возврат к свободному редактированию после структурированных секций (0005).
Теперь каждый воркер хранит ровно два текста: initial_template и
reply_template. Оператор пишет что хочет, расставляет плейсхолдеры
(`{current_time}`, `{conversation_history}`, `{partner_name}` и др.),
система при генерации подставляет реальные значения.

Старые данные стираются полностью — оператор начинает с пустого, как
просил.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-27

"""
from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


_OLD_COLUMNS = [
    "fabula", "bio", "style", "forbidden", "length_hint",
    "goals", "format_reply", "examples", "initial_system",
]

_NEW_COLUMNS = ["initial_template", "reply_template"]


def upgrade() -> None:
    for col in _OLD_COLUMNS:
        op.execute(f"ALTER TABLE account_prompts DROP COLUMN IF EXISTS {col};")
    for col in _NEW_COLUMNS:
        op.execute(
            f"ALTER TABLE account_prompts ADD COLUMN {col} TEXT NOT NULL DEFAULT '';"
        )


def downgrade() -> None:
    for col in reversed(_NEW_COLUMNS):
        op.execute(f"ALTER TABLE account_prompts DROP COLUMN IF EXISTS {col};")
    for col in _OLD_COLUMNS:
        op.execute(
            f"ALTER TABLE account_prompts ADD COLUMN {col} TEXT NOT NULL DEFAULT '';"
        )
