"""Persist reply progress without replaying historical sessions.

Revision ID: 0027
Revises: 0026
"""
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    # Unknown discovery time remains unknown for old messages.
    op.execute("ALTER TABLE messages ADD COLUMN saved_at timestamptz")
    op.execute("ALTER TABLE messages ALTER COLUMN saved_at SET DEFAULT NOW()")
    op.execute("ALTER TABLE dialogs ADD COLUMN status_source text")
    op.execute("ALTER TABLE dialogs ADD COLUMN status_source_message_id bigint")
    op.execute("""
        CREATE TABLE autochat_reply_batches (
            id bigserial PRIMARY KEY,
            session_id bigint NOT NULL REFERENCES autochat_sessions(id) ON DELETE CASCADE,
            dialog_id bigint NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
            account_id bigint NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            inbound_message_id bigint,
            phase text NOT NULL CHECK (phase IN
                ('generating','ready','sending','finishing','complete','superseded','review')),
            segments jsonb NOT NULL DEFAULT '[]',
            next_index integer NOT NULL DEFAULT 0,
            finish_requested boolean NOT NULL DEFAULT false,
            parent_id text,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT NOW(),
            updated_at timestamptz NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""CREATE UNIQUE INDEX uq_reply_pending_dialog
        ON autochat_reply_batches(dialog_id)
        WHERE phase IN ('generating','ready','sending','finishing','review')""")
    op.execute("CREATE INDEX ix_reply_session ON autochat_reply_batches(session_id, id DESC)")
    op.execute("CREATE INDEX ix_reply_dialog ON autochat_reply_batches(dialog_id, id DESC)")
    # Keep the entire revision atomic: startup must not leave added columns
    # behind with an old Alembic revision if index creation is interrupted.
    op.execute("CREATE INDEX IF NOT EXISTS media_message_idx ON media(message_id)")
    op.execute("""INSERT INTO settings(key,value) VALUES
        ('autochat.reply_journal_enabled','false'),
        ('autochat.recovery_enabled','false') ON CONFLICT(key) DO NOTHING""")


def downgrade():
    # Deliberately refuse destructive loss of unconfirmed delivery evidence.
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM autochat_reply_batches WHERE phase NOT IN ('complete','superseded'))
        THEN RAISE EXCEPTION 'Unresolved reply batches: preserve journal before downgrade'; END IF;
        END $$""")
    op.execute("DROP TABLE autochat_reply_batches")
    op.execute("ALTER TABLE dialogs DROP COLUMN status_source_message_id, DROP COLUMN status_source")
    op.execute("ALTER TABLE messages DROP COLUMN saved_at")
    op.execute("DELETE FROM settings WHERE key IN ('autochat.reply_journal_enabled','autochat.recovery_enabled')")
