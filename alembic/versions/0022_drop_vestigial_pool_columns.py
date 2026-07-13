"""drop_vestigial_pool_columns — убрать наследие claimed_at-модели пула.

После перехода на простую модель `claim = DELETE` (2026-07-13) колонки
`crm_lead_pool.claimed_at`, `assigned_account_id` (0018) и `attempts` (0016), а
также частичный индекс `crm_lead_pool_free_idx (... WHERE claimed_at IS NULL)`
кодом не читаются и не пишутся — простой claim удаляет забранного лида, поэтому
все строки свободны, зависших claim'ов не бывает, счётчик попыток не нужен.
Дропаем балласт.

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-13
"""
from __future__ import annotations

from alembic import op


revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS crm_lead_pool_free_idx;
        ALTER TABLE crm_lead_pool
            DROP COLUMN IF EXISTS claimed_at,
            DROP COLUMN IF EXISTS assigned_account_id,
            DROP COLUMN IF EXISTS attempts;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE crm_lead_pool
            ADD COLUMN attempts            INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN claimed_at          TIMESTAMPTZ,
            ADD COLUMN assigned_account_id INTEGER;
        CREATE INDEX crm_lead_pool_free_idx
            ON crm_lead_pool (gender, fetched_at)
            WHERE claimed_at IS NULL;
        """
    )
