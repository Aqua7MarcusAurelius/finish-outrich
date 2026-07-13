"""contacted_leads — глобальный реестр «этому человеку уже писали»

Корень кросс-аккаунт дублей («2 воркера — один лид»): дедуп при
create_session был per-account (`WHERE account_id=$1 AND telegram_user_id=$2`),
поэтому РАЗНЫЕ аккаунты свободно писали одному человеку. Плюс уже-прописанный
лид возвращался в свободный пул (reinsert при фейле сессии, реапер, партнёр
переназначал) и его подхватывал другой аккаунт.

Решение (простая логика): единый реестр `contacted_leads`. На успешной
отправке первого сообщения → INSERT. `claim_lead_for_account` и рефилл
`_take_into_pool` ПРОПУСКАЮТ любого, кто уже в реестре. Итог: прописали раз
→ больше никогда, любым аккаунтом, при любом рестарте/реинсерте.

Реестр БЭКФИЛИТСЯ из `autochat_sessions`: при сбросе системы переписки
удаляются (wipe), но записи сессий СОХРАНЯЮТСЯ (FK dialog_id = SET NULL),
поэтому история «кому писали» цела. Переносим всех target_username в реестр
— так все, кому когда-либо писали, защищены от повторной прописи любым
аккаунтом, а не только новые контакты. Дальше реестр пополняет create_session.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-13
"""
from __future__ import annotations

from alembic import op


revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS contacted_leads (
            username         TEXT PRIMARY KEY,
            telegram_user_id BIGINT,
            account_id       INTEGER,
            contacted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS contacted_leads_tg_user_idx
            ON contacted_leads (telegram_user_id);

        -- Бэкфилл: все, кому уже писали (по сохранённым autochat_sessions).
        -- lower(username) — реестр ключуется в нижнем регистре (дедуп
        -- сравнивает lower()). Берём самую раннюю сессию на username.
        INSERT INTO contacted_leads (username, telegram_user_id, account_id, contacted_at)
        SELECT DISTINCT ON (lower(target_username))
               lower(target_username), telegram_user_id, account_id, created_at
        FROM autochat_sessions
        WHERE COALESCE(target_username, '') <> ''
        ORDER BY lower(target_username), created_at ASC
        ON CONFLICT (username) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX IF EXISTS contacted_leads_tg_user_idx;
        DROP TABLE IF EXISTS contacted_leads;
    """)
