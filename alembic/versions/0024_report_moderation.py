"""report_moderation — модерация успешных диалогов кнопками в TG-группе

Раньше: оценщик решил `success` → пузырь «Завершен успешно» (терминальный)
→ finalizer сразу пушил `ready_for_transfer` в партнёрскую CRM. Человек
в решении не участвовал, ошибку оценщика уже не отыграть — API партнёра
запрещает менять статус лида, который попал в финал.

Теперь: успех уходит в модерацию. Бот публикует отчёт в группу семейства
с тремя кнопками (Валид беру / Валид не беру / Невалид). Нажатие ставит
галочку и запускает 4-часовой таймер; перевыбор сдвигает таймер. Когда
таймер истёк — статус уходит в CRM, кнопки снимаются, в подпись
дописывается итог. Без единого нажатия сообщение висит бесконечно.

Три части:
  1. `report_families` — карта семейство → (chat_id группы, l2_login).
     Переезд из `.env:REPORT_GROUP_MAP`: раньше добавление семейства
     требовало пересборки app (дважды ловили: EKSTAZ, ALADIN), теперь
     это INSERT. `l2_login` — кому назначать лида при «Валид, беру».
  2. `report_moderation` — состояние модерации. Таймер живёт здесь,
     а не в памяти процесса, поэтому переживает рестарт app.
  3. Пузырь «Модерация» — НЕ терминальный (`partner_terminal_status`
     = NULL), поэтому `partner_crm/finalizer.py` его игнорирует и
     ничего в CRM не пушит до решения человека.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-23
"""
from __future__ import annotations

from alembic import op


revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        -- ── 1. Карта семейств ────────────────────────────────────────
        CREATE TABLE report_families (
            family      TEXT PRIMARY KEY,
            chat_id     BIGINT NOT NULL,
            l2_login    TEXT,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        COMMENT ON TABLE  report_families IS
            'Семейство воркеров → группа результатов + L2 для assign-l2';
        COMMENT ON COLUMN report_families.l2_login IS
            'Логин L2 в партнёрской CRM. NULL = «Валид, беру» отработает '
            'как «не беру» (ready_for_transfer без назначения)';

        -- Сид: chat_id перенесены из .env REPORT_GROUP_MAP, l2_login
        -- назначены владельцем 2026-08-23. Четыре семейства без L2 —
        -- допишутся отдельным UPDATE, рестарт не потребуется.
        INSERT INTO report_families (family, chat_id, l2_login) VALUES
            ('SAD',     -5240340499,   'l2_001'),
            ('JOKER',   -4998104678,   'l2_005'),
            ('PIPITO',  -5268428873,   'l2_006'),
            ('KALAW',   -5282814129,   'l2_021'),
            ('AFERIST', -5068029574,   'l2_033'),
            ('FACE',    -5208145919,   'l2_036'),
            ('EKSTAZ',  -1003876195099,'l2_050'),
            ('YASYA',   -5461673139,   'l2_051'),
            ('BANAN',   -5578173185,   NULL),
            ('ALADIN',  -5126968676,   NULL),
            ('EFILJI',  -5480068446,   NULL),
            ('DONBAS',  -5586457332,   NULL),
            ('ONTON',   -5196079459,   NULL)
        ON CONFLICT (family) DO NOTHING;

        -- ── 2. Состояние модерации ───────────────────────────────────
        CREATE TABLE report_moderation (
            dialog_id       INTEGER PRIMARY KEY,
            username        TEXT NOT NULL,
            account_id      INTEGER,
            family          TEXT,
            chat_id         BIGINT NOT NULL,
            message_id      BIGINT NOT NULL,
            minio_key       TEXT,
            choice          TEXT,
            chosen_by       BIGINT,
            chosen_by_name  TEXT,
            chosen_at       TIMESTAMPTZ,
            push_after      TIMESTAMPTZ,
            pushed_at       TIMESTAMPTZ,
            pushed_status   TEXT,
            push_error      TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT report_moderation_choice_chk
                CHECK (choice IS NULL OR choice IN ('take', 'no_take', 'invalid'))
        );

        COMMENT ON COLUMN report_moderation.choice IS
            'take = Валид беру (assign-l2) | no_take = Валид не беру '
            '(ready_for_transfer) | invalid = Невалид. NULL = ещё не жали';
        COMMENT ON COLUMN report_moderation.push_after IS
            'chosen_at + 4ч. NULL пока не нажимали — сообщение висит вечно';

        -- Горячий путь тикера: «кому пора пушить». Частичный индекс —
        -- строки после пуша из него выпадают.
        CREATE INDEX report_moderation_due_idx
            ON report_moderation (push_after)
            WHERE pushed_at IS NULL AND push_after IS NOT NULL;

        -- Обработчик нажатия ищет строку по (chat_id, message_id).
        CREATE UNIQUE INDEX report_moderation_msg_idx
            ON report_moderation (chat_id, message_id);

        -- ── 3. Пузырь «Модерация» ────────────────────────────────────
        -- partner_terminal_status = NULL — это ключевое: finalizer шлёт
        -- в CRM только пузыри с непустым terminal_status, поэтому пока
        -- диалог в модерации партнёр видит его как in_progress.
        --
        -- UNIQUE(label) в таблице нет, поэтому идемпотентность —
        -- через WHERE NOT EXISTS, а не ON CONFLICT. Сдвиг sort_order
        -- тоже внутри условия, иначе повторный прогон сдвинет дважды.
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM dialog_statuses WHERE label = 'Модерация') THEN
                -- Освобождаем место: Модерация встаёт перед терминальными.
                UPDATE dialog_statuses SET sort_order = sort_order + 1
                WHERE sort_order >= 5;

                INSERT INTO dialog_statuses
                    (label, color, partner_terminal_status, sort_order)
                VALUES ('Модерация', '#a855f7', NULL, 5);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM dialog_statuses WHERE label = 'Модерация') THEN
                DELETE FROM dialog_statuses WHERE label = 'Модерация';
                UPDATE dialog_statuses SET sort_order = sort_order - 1
                WHERE sort_order >= 6;
            END IF;
        END $$;

        DROP INDEX IF EXISTS report_moderation_msg_idx;
        DROP INDEX IF EXISTS report_moderation_due_idx;
        DROP TABLE IF EXISTS report_moderation;
        DROP TABLE IF EXISTS report_families;
        """
    )
