"""account_evaluator_prompt — per-worker промт оценщика лидов.

До этого промт `lead_evaluator` был один на всю систему (файл
`prompts/lead_evaluator_system.md`). Для второго поставщика лидов под
другие задачи нужен СВОЙ оценщик с другими критериями «успеха». Даём
per-account переопределение промта — по образцу `initial_template` /
`reply_template`, которые уже живут в этой же таблице.

Пустая строка = поведение как раньше: используется глобальный файл-промт.
Существующие воркеры не затрагиваются (DEFAULT '' у всех).

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-20
"""
from __future__ import annotations

from alembic import op


revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE account_prompts
            ADD COLUMN IF NOT EXISTS evaluator_template TEXT NOT NULL DEFAULT '';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE account_prompts
            DROP COLUMN IF EXISTS evaluator_template;
        """
    )
