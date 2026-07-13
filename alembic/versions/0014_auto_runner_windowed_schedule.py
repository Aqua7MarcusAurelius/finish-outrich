"""auto_runner_windowed_schedule — переход с (interval + jitter) на окно + N лидов

Старая модель автопрописи: `interval_sec` + `jitter_sec` — каждые ~20 мин
±5 мин ставим один лид, крутится бесконечно. Проблема: расписание
равномерно-периодическое, легко детектится как бот; в очереди пусто —
воркер всё равно тикает с одинаковым ритмом.

Новая модель: в момент включения автопрописи (`start()`) фиксируется
СЕССИЯ на `window_hours` (дефолт 12ч), внутри окна случайным образом
раскидывается N лидов (N ∈ [min_leads, max_leads]). Первый лид — не
раньше чем через `warmup_minutes` (дефолт 10) после старта, чтобы не
палить включение. Расписание пишется в `schedule TIMESTAMPTZ[]`, каждый
раз при обработке момент удаляется. Когда массив пуст ИЛИ прошло
`session_ends_at` — авто-стоп с reason=`session_completed`.

`reset_on_boot` теперь НЕ гасит is_running: расписание в БД, автоматы
поднимаются на оставшихся timestamp'ах после рестарта app.

Удаляем: `interval_sec`, `jitter_sec` и связанные CHECK'и.
Добавляем: `window_hours`, `min_leads`, `max_leads`, `warmup_minutes`,
           `session_started_at`, `session_ends_at`, `schedule`.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-30

"""
from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE worker_auto_runners
            DROP CONSTRAINT IF EXISTS auto_runner_interval_min,
            DROP CONSTRAINT IF EXISTS auto_runner_jitter_nonneg;

        ALTER TABLE worker_auto_runners
            DROP COLUMN IF EXISTS interval_sec,
            DROP COLUMN IF EXISTS jitter_sec;

        ALTER TABLE worker_auto_runners
            ADD COLUMN window_hours       INTEGER NOT NULL DEFAULT 12,
            ADD COLUMN min_leads          INTEGER NOT NULL DEFAULT 15,
            ADD COLUMN max_leads          INTEGER NOT NULL DEFAULT 25,
            ADD COLUMN warmup_minutes     INTEGER NOT NULL DEFAULT 10,
            ADD COLUMN session_started_at TIMESTAMPTZ,
            ADD COLUMN session_ends_at    TIMESTAMPTZ,
            ADD COLUMN schedule           TIMESTAMPTZ[] NOT NULL DEFAULT '{}';

        ALTER TABLE worker_auto_runners
            ADD CONSTRAINT auto_runner_window_positive
                CHECK (window_hours >= 1 AND window_hours <= 48),
            ADD CONSTRAINT auto_runner_lead_range
                CHECK (min_leads >= 1 AND max_leads >= min_leads AND max_leads <= 500),
            ADD CONSTRAINT auto_runner_warmup_nonneg
                CHECK (warmup_minutes >= 0 AND warmup_minutes <= 240);
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE worker_auto_runners
            DROP CONSTRAINT IF EXISTS auto_runner_window_positive,
            DROP CONSTRAINT IF EXISTS auto_runner_lead_range,
            DROP CONSTRAINT IF EXISTS auto_runner_warmup_nonneg;

        ALTER TABLE worker_auto_runners
            DROP COLUMN IF EXISTS schedule,
            DROP COLUMN IF EXISTS session_ends_at,
            DROP COLUMN IF EXISTS session_started_at,
            DROP COLUMN IF EXISTS warmup_minutes,
            DROP COLUMN IF EXISTS max_leads,
            DROP COLUMN IF EXISTS min_leads,
            DROP COLUMN IF EXISTS window_hours;

        ALTER TABLE worker_auto_runners
            ADD COLUMN interval_sec INTEGER NOT NULL DEFAULT 1200,
            ADD COLUMN jitter_sec   INTEGER NOT NULL DEFAULT 300;

        ALTER TABLE worker_auto_runners
            ADD CONSTRAINT auto_runner_interval_min  CHECK (interval_sec >= 60),
            ADD CONSTRAINT auto_runner_jitter_nonneg CHECK (jitter_sec >= 0);
    """)
