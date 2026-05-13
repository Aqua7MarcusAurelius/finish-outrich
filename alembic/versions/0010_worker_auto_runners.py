"""worker_auto_runners — авторежим старта диалогов из очереди юзернеймов

Per-account конфиг авторежима: список юзернеймов, интервал между
стартами автодиалогов с jitter, флаг включён/выключен. Один ряд на воркер
(PK = account_id), CASCADE при удалении аккаунта.

После рестарта `app` поднимать ничего автоматически не нужно — флаг
`is_running` сбрасывается в FALSE при старте сервиса (см.
modules/worker_auto_runner/service.py::reset_on_boot). Оператор после
рестарта включает заново вручную.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-13

"""
from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE worker_auto_runners (
            account_id      INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
            is_running      BOOLEAN NOT NULL DEFAULT FALSE,
            queue           TEXT[] NOT NULL DEFAULT '{}',
            interval_sec    INTEGER NOT NULL DEFAULT 1200,
            jitter_sec      INTEGER NOT NULL DEFAULT 300,
            last_action_at  TIMESTAMPTZ,
            last_status     TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT auto_runner_interval_min  CHECK (interval_sec >= 60),
            CONSTRAINT auto_runner_jitter_nonneg CHECK (jitter_sec >= 0),
            CONSTRAINT auto_runner_queue_size    CHECK (
                array_length(queue, 1) IS NULL OR array_length(queue, 1) <= 1000
            )
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS worker_auto_runners;")
