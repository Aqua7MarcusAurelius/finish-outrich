"""Отделить «пузырь закрывает диалог» от «что слать в CRM».

Построитель отчётов (`autochat/report_consumer`) решал, пора ли строить
отчёт, по `partner_terminal_status IS NOT NULL`. Пока терминальность
пузыря и финальность статуса в CRM совпадали, это работало. Миграция
0024 их развела: «Модерация» закрывает диалог, но в CRM ничего не шлёт
(статус ставит модератор кнопкой). В результате успешные диалоги
перестали попадать в отчёты вообще — а значит и в TG-группы.

Заводим отдельный флаг `closes_dialog`: диалог доведён до конца, отчёт
пора строить. `partner_terminal_status` остаётся ровно тем, чем был —
кодом для партнёрской CRM.

Revision ID: 0026
Revises: 0025
"""
from __future__ import annotations

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE dialog_statuses
            ADD COLUMN IF NOT EXISTS closes_dialog BOOLEAN NOT NULL DEFAULT FALSE
        """
    )
    # Всё, что раньше считалось терминальным (есть код для CRM), плюс
    # «Модерация» — она закрывает диалог, но статус шлёт не сама.
    op.execute(
        """
        UPDATE dialog_statuses
        SET closes_dialog = TRUE
        WHERE partner_terminal_status IS NOT NULL
           OR label = 'Модерация'
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE dialog_statuses DROP COLUMN IF EXISTS closes_dialog")
