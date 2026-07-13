"""contacted_leads: durable-бэкфилл из events_archive (а не из autochat_sessions).

КОРНЕВОЙ фикс «повторного прописывания» (2026-07-13). Реестр дедупа
`contacted_leads` изначально (0019) бэкфилился из `autochat_sessions`. Но сессии
УДАЛЯЮТСЯ вместе с диалогами (wipe диалога, сброс системы, FK dialog_id → …),
поэтому реестр терял записи о реально написанных людях. На проде это дало разрыв
~69%: 1520 в реестре против 4845 фактических контактов в логе. Из-за этого
партнёр переназначал уже-написанных, а дедуп их не узнавал → лид «прописывался
по второму кругу».

`events_archive` — append-only durable-лог; `autochat.started` публикуется на
каждый старт автодиалога и содержит `username` + `telegram_user_id`. Он переживает
удаление сессий/диалогов, поэтому это ПРАВИЛЬНЫЙ источник для реестра дедупа.

Бэкфиллим `contacted_leads` из `autochat.started` (ключ — самый ранний контакт на
username). Идемпотентно (ON CONFLICT DO NOTHING) — уже существующие записи
(в т.ч. рантайм-инсерты из create_session) не трогаем.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-13
"""
from __future__ import annotations

from alembic import op


revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO contacted_leads (username, telegram_user_id, account_id, contacted_at)
        SELECT DISTINCT ON (lower(data->>'username'))
               lower(data->>'username'),
               NULLIF(data->>'telegram_user_id', '')::bigint,
               NULLIF(data->>'account_id', '')::int,
               time
        FROM events_archive
        WHERE type = 'autochat.started'
          AND COALESCE(data->>'username', '') <> ''
        ORDER BY lower(data->>'username'), time ASC
        ON CONFLICT (username) DO NOTHING;
        """
    )


def downgrade() -> None:
    # Данные восстановлению не подлежат и не должны — это реестр «кому писали».
    pass
