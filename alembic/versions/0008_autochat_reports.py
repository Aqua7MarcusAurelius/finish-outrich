"""autochat_reports — HTML-отчёты по завершённым LLM-стопом сессиям

Таблица хранит ссылку на HTML-файл отчёта в MinIO (бакет
`MINIO_REPORTS_BUCKET`, по умолчанию `autochat-reports`). Отчёт
создаётся при `<finishdialog/>` — единственном "штатном" сценарии
завершения, по которому есть смысл сохранять выгрузку для оператора.

Один отчёт на сессию (UNIQUE session_id). Каскад при удалении сессии.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-11

"""
from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE autochat_reports (
            id              BIGSERIAL PRIMARY KEY,
            session_id      INTEGER NOT NULL UNIQUE
                            REFERENCES autochat_sessions(id) ON DELETE CASCADE,
            minio_key       TEXT NOT NULL,
            messages_count  INTEGER NOT NULL DEFAULT 0,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS autochat_reports;")
