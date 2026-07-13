"""account_fingerprint — индивидуальные api_id/api_hash + device-info на аккаунт

До этой миграции воркер при подключении к Telegram использовал глобальные
`TELEGRAM_API_ID` / `TELEGRAM_API_HASH` из `.env` и дефолтный device-fingerprint
Telethon (одни и те же значения на всю ферму). Это маркер автоматизации, по
которому Telegram легко группирует аккаунты и применяет batch-ban'ы.

Колонки добавляются NULLABLE: служебный аккаунт доставки отчётов остаётся
без fingerprint'а (он не работает через стандартный wrapper), а workers без
заполненных полей не смогут подняться — wrapper валидирует наличие
api_id/api_hash и значимых device-полей перед connect.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-27

"""
from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE accounts
            ADD COLUMN api_id           INTEGER,
            ADD COLUMN api_hash         TEXT,
            ADD COLUMN device_model     TEXT,
            ADD COLUMN system_version   TEXT,
            ADD COLUMN app_version      TEXT,
            ADD COLUMN lang_code        TEXT,
            ADD COLUMN system_lang_code TEXT,
            ADD COLUMN lang_pack        TEXT;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE accounts
            DROP COLUMN IF EXISTS lang_pack,
            DROP COLUMN IF EXISTS system_lang_code,
            DROP COLUMN IF EXISTS lang_code,
            DROP COLUMN IF EXISTS app_version,
            DROP COLUMN IF EXISTS system_version,
            DROP COLUMN IF EXISTS device_model,
            DROP COLUMN IF EXISTS api_hash,
            DROP COLUMN IF EXISTS api_id;
    """)
