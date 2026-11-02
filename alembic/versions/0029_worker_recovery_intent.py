"""Persist explicit worker start/stop intent, without enabling old accounts.

Revision ID: 0029
Revises: 0028
"""
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE accounts ADD COLUMN worker_desired_running "
        "BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE accounts DROP COLUMN worker_desired_running")
