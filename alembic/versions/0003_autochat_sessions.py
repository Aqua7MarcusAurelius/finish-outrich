"""autochat_sessions + autochat settings

Добавляет таблицу autochat_sessions для модуля автообщения (Opus 4.7)
и 7 настроек в settings с дефолтными значениями таймингов.

Partial unique index по (account_id, telegram_user_id) для активных
статусов (active/paused) — не даём завести две активные сессии на одну
и ту же пару.

См. docs/autochat.md.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-22

"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─────────────────────────────────────────────────────────────────
    # autochat_sessions — инициированные нами переписки
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE autochat_sessions (
            id                      SERIAL PRIMARY KEY,
            account_id              INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            dialog_id               INTEGER REFERENCES dialogs(id) ON DELETE SET NULL,
            telegram_user_id        BIGINT NOT NULL,
            target_username         TEXT NOT NULL,
            system_prompt           TEXT NOT NULL,
            initial_prompt          TEXT NOT NULL,
            initial_sent_text       TEXT,
            status                  TEXT NOT NULL DEFAULT 'starting',
            in_chat                 BOOLEAN NOT NULL DEFAULT FALSE,
            last_our_activity_at    TIMESTAMPTZ,
            last_their_message_at   TIMESTAMPTZ,
            last_any_message_at     TIMESTAMPTZ,
            last_error              TEXT,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT autochat_status_chk CHECK (
                status IN ('starting', 'active', 'paused', 'failed', 'stopped')
            )
        );
    """)

    # Индекс для быстрого поиска активных сессий при старте и при получении
    # входящих событий (service.py фильтрует по dialog_id активных).
    op.execute("""
        CREATE INDEX autochat_sessions_active_idx
        ON autochat_sessions (account_id, status)
        WHERE status IN ('active', 'paused');
    """)

    op.execute("CREATE INDEX autochat_sessions_dialog_idx ON autochat_sessions (dialog_id);")

    # Partial unique: одна активная/приостановленная сессия на пару
    # (account_id, telegram_user_id). Завершённые (failed/stopped) не
    # мешают завести новую.
    op.execute("""
        CREATE UNIQUE INDEX autochat_sessions_active_pair_uniq
        ON autochat_sessions (account_id, telegram_user_id)
        WHERE status IN ('active', 'paused', 'starting');
    """)

    # ─────────────────────────────────────────────────────────────────
    # Настройки автообщения — тюнятся без рестарта
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        INSERT INTO settings (key, value, description) VALUES
            ('autochat.enter_delay_short_sec', '15',  'Задержка входа в чат при возрасте последнего сообщения 0-5 мин'),
            ('autochat.enter_delay_mid_sec',   '60',  'То же для 5-10 мин'),
            ('autochat.enter_delay_long_sec',  '120', 'То же для ≥10 мин'),
            ('autochat.idle_leave_sec',        '180', 'Через сколько тишины уходим в InChat=0 (3 мин)'),
            ('autochat.reply_timer_sec',       '30',  'Базовый reply-таймер перед запросом в LLM'),
            ('autochat.openrouter_retries',    '2',   'Ретраи при ошибке OpenRouter в автодиалогах'),
            ('autochat.typing_ms_per_char',    '40',  'Имитация печати — миллисекунд на символ')
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("DELETE FROM settings WHERE key LIKE 'autochat.%';")
    op.execute("DROP TABLE IF EXISTS autochat_sessions;")
