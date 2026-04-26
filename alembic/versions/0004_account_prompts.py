"""account_prompts — per-worker промты для AutoChat

Отдельная таблица (а не колонки в accounts) — на будущее под версионирование
и историю правок промтов. Один промт на воркер: PK = account_id (1:1).

Пустой reply_system блокирует генерацию автоответов на стороне модуля
autochat (см. session.py::_generate_and_enqueue). Пустой initial_system
блокирует POST /autochat/start.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-26

"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE account_prompts (
            account_id      INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
            reply_system    TEXT NOT NULL DEFAULT '',
            initial_system  TEXT NOT NULL DEFAULT '',
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS account_prompts;")
