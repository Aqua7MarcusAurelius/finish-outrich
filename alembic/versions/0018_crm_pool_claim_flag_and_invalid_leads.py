"""crm_pool_claim_flag_and_invalid_leads — Fix #13 + Fix #14

Fix #13: claim_lead_for_account удалял запись из пула. Reinsert потом
делал INSERT ON CONFLICT DO UPDATE — но ROW не было в пуле, поэтому
attempts всегда ставился в 1. Из-за этого max-attempts защита из Fix #8
не работала — лид мог крутиться в цикле бесконечно.

Правильная схема: claim НЕ удаляет запись, а помечает `claimed_at =
NOW()` + `assigned_account_id`. `claim_lead_for_account` фильтрует по
`claimed_at IS NULL` через FOR UPDATE SKIP LOCKED. `reinsert_into_pool`
делает UPDATE claimed_at=NULL, attempts=attempts+1. Настоящий DELETE
только при финализации (push invalid / успешный dialog started).

Fix #14: новая таблица `invalid_leads (username, invalidated_at)`.
Poller при refill из партнёрской CRM НЕ добавляет в пул то что там
есть — партнёр может сколько угодно раз возвращать нам известного
мёртвого лида, мы больше на него не купимся.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-12
"""
from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        -- Fix #13: claimed_at + assigned_account_id
        ALTER TABLE crm_lead_pool
            ADD COLUMN claimed_at          TIMESTAMPTZ,
            ADD COLUMN assigned_account_id INTEGER;

        CREATE INDEX crm_lead_pool_free_idx
            ON crm_lead_pool (gender, fetched_at)
            WHERE claimed_at IS NULL;

        -- Fix #14: перманентный чёрный список
        CREATE TABLE invalid_leads (
            username        TEXT PRIMARY KEY,
            reason          TEXT,
            invalidated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            attempts_seen   INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX invalid_leads_by_time ON invalid_leads (invalidated_at DESC);

        -- Seed: перенести все уже pushed'нутые invalid из events_archive
        -- за последние 30 дней, чтобы партнёр не смог их подсунуть
        -- сразу после апгрейда.
        INSERT INTO invalid_leads (username, reason, invalidated_at)
        SELECT DISTINCT ON (data->>'username')
               data->>'username',
               'seeded_from_history',
               time
        FROM events_archive
        WHERE type='partner_crm.status_pushed'
          AND data->>'status' = 'invalid'
          AND time >= NOW() - INTERVAL '30 days'
          AND data->>'username' IS NOT NULL
        ORDER BY data->>'username', time DESC
        ON CONFLICT (username) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX IF EXISTS invalid_leads_by_time;
        DROP TABLE IF EXISTS invalid_leads;
        DROP INDEX IF EXISTS crm_lead_pool_free_idx;
        ALTER TABLE crm_lead_pool
            DROP COLUMN IF EXISTS assigned_account_id,
            DROP COLUMN IF EXISTS claimed_at;
    """)
