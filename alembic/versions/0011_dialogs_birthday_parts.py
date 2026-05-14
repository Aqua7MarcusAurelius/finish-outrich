"""dialogs.birthday → birthday_day / birthday_month / birthday_year

Telegram отдаёт день рождения как `{day, month, year?}` — год опционален.
Колонка `birthday DATE` для этого не годится (DATE требует год), поэтому
расщепляем на три SMALLINT поля. Поле раньше никем не заполнялось —
данных, которые нужно мигрировать, нет.

`birthday_year` NULLable: год хранится только когда собеседник его указал.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-14

"""
from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE dialogs DROP COLUMN IF EXISTS birthday;")
    op.execute("""
        ALTER TABLE dialogs
            ADD COLUMN birthday_day   SMALLINT,
            ADD COLUMN birthday_month SMALLINT,
            ADD COLUMN birthday_year  SMALLINT;
    """)
    # Семантика: либо есть и day и month (с опц. year), либо нет ничего.
    # Половинных дат не бывает.
    op.execute("""
        ALTER TABLE dialogs ADD CONSTRAINT dialogs_birthday_chk CHECK (
            (birthday_day IS NULL AND birthday_month IS NULL)
            OR (
                birthday_day BETWEEN 1 AND 31
                AND birthday_month BETWEEN 1 AND 12
                AND (birthday_year IS NULL OR birthday_year BETWEEN 1900 AND 2100)
            )
        );
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE dialogs DROP CONSTRAINT IF EXISTS dialogs_birthday_chk;")
    op.execute("""
        ALTER TABLE dialogs
            DROP COLUMN IF EXISTS birthday_year,
            DROP COLUMN IF EXISTS birthday_month,
            DROP COLUMN IF EXISTS birthday_day;
    """)
    op.execute("ALTER TABLE dialogs ADD COLUMN birthday DATE;")
