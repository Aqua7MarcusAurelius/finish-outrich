"""drop events_archive.account_id FK

События шины могут ссылаться на account_id которого ещё нет в accounts
(например, событие account.created приходит ДО того как мы сами его запишем;
или system.error может прилететь ссылаясь на уже удалённый аккаунт раньше
чем ON DELETE SET NULL успеет сработать в одной транзакции).

Архив событий — не операционные данные, FK тут только мешает вставкам.
Оставляем колонку как обычный INTEGER без FK.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-21

"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE events_archive "
        "DROP CONSTRAINT IF EXISTS events_archive_account_id_fkey"
    )


def downgrade() -> None:
    # Восстановление FK возможно только если все существующие account_id
    # есть в accounts — иначе откат упадёт. Это ожидаемо.
    op.execute(
        "ALTER TABLE events_archive "
        "ADD CONSTRAINT events_archive_account_id_fkey "
        "FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL"
    )
