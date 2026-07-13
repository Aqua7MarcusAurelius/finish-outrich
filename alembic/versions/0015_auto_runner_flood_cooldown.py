"""auto_runner_flood_cooldown — cooldown после PEER_FLOOD / FloodWait

При срабатывании транзиентного flood-ошибки от TG (`PEER_FLOOD` = "Too
many requests" без секунд, либо `FloodWaitError` с явными секундами)
воркер отправляет @SpamBot команду `/start` дважды (эта операция снимает
флуд у Premium-аккаунтов), после чего auto_runner ставит паузу
`cooldown_until = NOW() + 2h` для этого account_id — расписание не
отматывается, но ни одна попытка отправки не идёт до истечения cooldown.

Поля переживают рестарт app'а (важно: `resume_running_loops` при старте
должен уважать cooldown_until и не пытаться слать пока пауза не истекла).

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-11

"""
from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE worker_auto_runners
            ADD COLUMN cooldown_until  TIMESTAMPTZ,
            ADD COLUMN cooldown_reason TEXT;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE worker_auto_runners
            DROP COLUMN IF EXISTS cooldown_reason,
            DROP COLUMN IF EXISTS cooldown_until;
    """)
