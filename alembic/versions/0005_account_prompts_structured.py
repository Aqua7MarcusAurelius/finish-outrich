"""account_prompts → структурированные поля для reply

Заменяет монолитный reply_system на 8 семантических колонок:
fabula, bio, style, forbidden, length_hint, goals, format_reply, examples.

Код в modules/autochat/prompts.py собирает из них system-промт с
заголовками секций. Пустые поля пропускаются. Все 8 пустые → гейт
блокирует генерацию (см. session.py).

initial_system остаётся как есть — initial вне скоупа этой переделки.

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-26

"""
from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


_NEW_COLUMNS = [
    "fabula",
    "bio",
    "style",
    "forbidden",
    "length_hint",
    "goals",
    "format_reply",
    "examples",
]


def upgrade() -> None:
    # Старая монолитная колонка уходит — данных в продакшене ещё нет.
    op.execute("ALTER TABLE account_prompts DROP COLUMN IF EXISTS reply_system;")
    for col in _NEW_COLUMNS:
        op.execute(
            f"ALTER TABLE account_prompts ADD COLUMN {col} TEXT NOT NULL DEFAULT '';"
        )


def downgrade() -> None:
    for col in reversed(_NEW_COLUMNS):
        op.execute(f"ALTER TABLE account_prompts DROP COLUMN IF EXISTS {col};")
    op.execute(
        "ALTER TABLE account_prompts ADD COLUMN reply_system TEXT NOT NULL DEFAULT '';"
    )
