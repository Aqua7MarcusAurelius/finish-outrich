"""Durable terminal CRM delivery receipts, without historical backfill.

Revision ID: 0028
Revises: 0027
"""
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE dialogs ADD COLUMN crm_finalization_version BIGINT NOT NULL DEFAULT 0;
        CREATE FUNCTION bump_crm_finalization_version() RETURNS trigger AS $$
        BEGIN
            IF NEW.status_id IS DISTINCT FROM OLD.status_id
               OR NEW.partner_crm_terminal_reason IS DISTINCT FROM OLD.partner_crm_terminal_reason
            THEN
                NEW.crm_finalization_version := OLD.crm_finalization_version + 1;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER dialogs_crm_finalization_version
            BEFORE UPDATE OF status_id, partner_crm_terminal_reason ON dialogs
            FOR EACH ROW EXECUTE FUNCTION bump_crm_finalization_version();

        CREATE TABLE partner_crm_finalizations (
            dialog_id INTEGER PRIMARY KEY REFERENCES dialogs(id) ON DELETE CASCADE,
            desired_version BIGINT NOT NULL,
            desired_status TEXT NOT NULL,
            source_status_id INTEGER,
            source_reason TEXT,
            account_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','sending','retry','confirmed','review','superseded')),
            attempts INTEGER NOT NULL DEFAULT 0,
            needs_reconcile BOOLEAN NOT NULL DEFAULT FALSE,
            next_attempt_at TIMESTAMPTZ,
            confirmed_status TEXT,
            confirmed_at TIMESTAMPTZ,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX partner_crm_finalizations_due_idx
            ON partner_crm_finalizations(next_attempt_at)
            WHERE state IN ('pending','sending','retry');
        INSERT INTO settings(key,value,description) VALUES
            ('partner_crm.finalizer_retry_enabled','false',
             'Retry only new terminal CRM journal entries; no historical backfill'),
            ('partner_crm.retry_owner_id','',
             'Verified CRM L1 user ID; missing/mismatching ownership blocks automatic retry')
        ON CONFLICT(key) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM settings WHERE key IN
            ('partner_crm.finalizer_retry_enabled','partner_crm.retry_owner_id');
        DROP TABLE partner_crm_finalizations;
        DROP TRIGGER dialogs_crm_finalization_version ON dialogs;
        DROP FUNCTION bump_crm_finalization_version();
        ALTER TABLE dialogs DROP COLUMN crm_finalization_version;
    """)
