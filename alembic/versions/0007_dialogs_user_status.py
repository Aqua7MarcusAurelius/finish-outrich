"""dialogs.user_status — пометка оператора (UI-only)

Свободный текстовый статус для удобства оператора. Никакой логикой
не используется — только отображается в списке диалогов и фильтрует
визуально. Допустимые значения определены в frontend (talking, waiting,
done, failed). NULL = "без статуса".

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-01

"""
from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE dialogs ADD COLUMN user_status TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE dialogs DROP COLUMN IF EXISTS user_status;")
