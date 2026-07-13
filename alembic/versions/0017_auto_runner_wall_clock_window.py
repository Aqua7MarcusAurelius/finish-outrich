"""auto_runner_wall_clock_window — часы дня для окна автопрописи (MSK)

Fix #9, 2026-07-11: раньше `window_hours` был просто длительность (12ч
от момента `start()`). Оператор просил жёстко разнести пропись по
времени суток — по МСК с 08:00 до 23:00. Ввели два поля:

  active_start_hour_msk INT (0..23, default 8)  — во сколько (MSK) окно открывается
  active_end_hour_msk   INT (1..24, default 23) — во сколько (MSK) окно закрывается

Расписание строится каждый раз на «сегодняшнее» окно (или на завтрашнее
если сейчас уже позже `active_end`). Внутри окна — те же `min_leads`,
`max_leads`, `warmup_minutes`.

Одновременно меняем дефолты `min_leads` 15→15 и `max_leads` 25→18
(оператор попросил чуть скромнее по объёму, чтобы реже триггерить
PEER_FLOOD).

`window_hours` **оставляем** — в коде теперь просто derived (для
внутренних расчётов), но UI и DTO больше про него не рассказывают.
Cleanup удалим отдельной миграцией когда убедимся что никто не использует.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-11

"""
from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE worker_auto_runners
            ADD COLUMN active_start_hour_msk INTEGER NOT NULL DEFAULT 8,
            ADD COLUMN active_end_hour_msk   INTEGER NOT NULL DEFAULT 23;

        ALTER TABLE worker_auto_runners
            ALTER COLUMN max_leads SET DEFAULT 18;

        -- Обновляем ВСЕ существующие ряды на новые дефолты, чтобы
        -- fresh-start после апгрейда сразу использовал новую конфигурацию.
        UPDATE worker_auto_runners
        SET active_start_hour_msk = 8,
            active_end_hour_msk   = 23,
            min_leads             = 15,
            max_leads             = 18,
            updated_at            = NOW();

        ALTER TABLE worker_auto_runners
            ADD CONSTRAINT auto_runner_active_hours CHECK (
                active_start_hour_msk BETWEEN 0 AND 23
                AND active_end_hour_msk BETWEEN 1 AND 24
                AND active_end_hour_msk > active_start_hour_msk
            );
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE worker_auto_runners
            DROP CONSTRAINT IF EXISTS auto_runner_active_hours;

        ALTER TABLE worker_auto_runners
            DROP COLUMN IF EXISTS active_end_hour_msk,
            DROP COLUMN IF EXISTS active_start_hour_msk;

        ALTER TABLE worker_auto_runners
            ALTER COLUMN max_leads SET DEFAULT 25;
    """)
