"""Ограничение ретраев отложенного пуша модерации.

До этой миграции `_push_due` выбирал строки по
`pushed_at IS NULL AND push_after <= NOW()`. Если CRM отвечала ошибкой,
`pushed_at` не проставлялся — и строка оставалась «просроченной» навсегда,
то есть тикер долбил партнёрский API раз в минуту бесконечно. Для
временного сбоя это правильно, для постоянного отказа (403 на лиде,
который у партнёра уже в финале) — примерно 1400 бесполезных запросов
в сутки на одну строку.

Добавляем счётчик попыток: тикер берёт строку только пока
`push_attempts < MAX_PUSH_ATTEMPTS`, между попытками сдвигает `push_after`
экспоненциальным backoff'ом. Исчерпав лимит, строка выпадает из выборки
и остаётся с непустым `push_error` — диалог виден оператору в пузыре
«Модерация», кнопки в группе живы, новое нажатие обнуляет счётчик и
запускает цикл заново.

Revision ID: 0025
Revises: 0024
"""
from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE report_moderation
            ADD COLUMN IF NOT EXISTS push_attempts INTEGER NOT NULL DEFAULT 0
        """
    )
    # Индекс под выборку тикера: только незакрытые строки со взведённым
    # таймером. Partial — таблица растёт, а «горячих» строк единицы.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS report_moderation_due_idx
            ON report_moderation (push_after)
            WHERE pushed_at IS NULL AND push_after IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS report_moderation_due_idx")
    op.execute("ALTER TABLE report_moderation DROP COLUMN IF EXISTS push_attempts")
