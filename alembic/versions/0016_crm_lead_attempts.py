"""crm_lead_pool.attempts — счётчик неудачных попыток на лида

При каждом reinsert в пул +1 к attempts (ON CONFLICT DO UPDATE). Когда
attempts >= MAX_LEAD_ATTEMPTS (3) — вместо возврата в пул лид
финализируется в партнёрскую CRM как `invalid` (мы не смогли выйти на
контакт после N попыток разных воркеров).

Fix #8, 2026-07-11 — см. запись в §20 SPEC.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-11

"""
from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE crm_lead_pool
            ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE crm_lead_pool
            DROP COLUMN IF EXISTS attempts;
    """)
