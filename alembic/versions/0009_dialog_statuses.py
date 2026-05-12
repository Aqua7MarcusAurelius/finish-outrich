"""dialog_statuses — каталог операторских статусов

Новая таблица `dialog_statuses` — каталог пузырей с произвольным
лейблом и цветом (hex `#RRGGBB`). Управляется через модуль
`modules/dialog_statuses/` (CRUD + публикация
`dialog_statuses.catalog_changed`).

Меняем `dialogs.user_status TEXT` (где раньше хранились хардкоженные
ключи talking/waiting/done/failed) на FK `status_id` на новый каталог.
При удалении статуса из каталога связанные диалоги получают
`status_id = NULL` (ON DELETE SET NULL) — операторская «пометка»
просто снимается, диалоги остаются.

Сид: 4 строки, соответствующие старым ключам. После переноса колонка
`user_status` дропается.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-12

"""
from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE dialog_statuses (
            id          SERIAL PRIMARY KEY,
            label       TEXT NOT NULL,
            color       TEXT NOT NULL,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT dialog_statuses_color_chk CHECK (color ~ '^#[0-9A-Fa-f]{6}$'),
            CONSTRAINT dialog_statuses_label_chk CHECK (length(label) BETWEEN 1 AND 32)
        );
    """)

    # Сидируем 4 текущих как обычные строки, без специального флага. Оператор
    # сможет переименовать, перекрасить или удалить их как любые другие.
    op.execute("""
        INSERT INTO dialog_statuses (id, label, color, sort_order) VALUES
            (1, 'Общаемся',   '#22c55e', 1),
            (2, 'Ждём ответ', '#eab308', 2),
            (3, 'Готово',     '#94a3b8', 3),
            (4, 'Провал',     '#ef4444', 4);
    """)
    # SERIAL уже на месте; смещаем последовательность чтобы следующий
    # вставленный id шёл с 5, а не с 1 (после явных id в INSERT).
    op.execute("SELECT setval('dialog_statuses_id_seq', 4, true);")

    op.execute("""
        ALTER TABLE dialogs
        ADD COLUMN status_id INTEGER
            REFERENCES dialog_statuses(id) ON DELETE SET NULL;
    """)
    op.execute("CREATE INDEX dialogs_status_idx ON dialogs (status_id) WHERE status_id IS NOT NULL;")

    # Маппим старые строковые значения user_status в новые FK.
    op.execute("""
        UPDATE dialogs SET status_id = CASE user_status
            WHEN 'talking' THEN 1
            WHEN 'waiting' THEN 2
            WHEN 'done'    THEN 3
            WHEN 'failed'  THEN 4
            ELSE NULL
        END
        WHERE user_status IS NOT NULL;
    """)

    op.execute("ALTER TABLE dialogs DROP COLUMN user_status;")


def downgrade() -> None:
    op.execute("ALTER TABLE dialogs ADD COLUMN user_status TEXT;")
    # Откат маппинга — пытаемся восстановить строковые значения для 4 сидов.
    # Если оператор успел добавить новых статусов — у диалогов с ними
    # user_status останется NULL (мы не сохраняем их custom label).
    op.execute("""
        UPDATE dialogs SET user_status = CASE status_id
            WHEN 1 THEN 'talking'
            WHEN 2 THEN 'waiting'
            WHEN 3 THEN 'done'
            WHEN 4 THEN 'failed'
            ELSE NULL
        END;
    """)
    op.execute("DROP INDEX IF EXISTS dialogs_status_idx;")
    op.execute("ALTER TABLE dialogs DROP COLUMN status_id;")
    op.execute("DROP TABLE IF EXISTS dialog_statuses;")
