"""crm_integration — интеграция с партнёрской CRM (этап 10)

Меняем модель работы с лидами: вместо ручной вбивки `@username`'ов
оператором подключаем партнёрский CRM как источник. Лид остаётся
диалогом — никаких отдельных таблиц `leads`.

Что в этой миграции:

1. `dialogs`:
   - `gender`        — пол лида ('female' | 'male' | NULL)
   - `from_crm`      — пришёл из партнёрского CRM (bool)
   - `partner_crm_terminal_reason` — какой код пушим в CRM
                       при финализации ('ready_for_transfer' /
                       'invalid' / 'no_response' / NULL)

2. `accounts`:
   - `lead_source`   — источник лидов для воркера ('manual' | 'partner_crm')
   - `target_gender` — фильтр пола для подбора из CRM-пула
                       ('any' | 'female' | 'male')

3. `dialog_statuses.partner_terminal_status` — если заполнено
   (одно из 'ready_for_transfer' / 'invalid' / 'no_response') →
   пузырь терминальный, при назначении триггерит push в CRM.
   Сиды переписываются с 4 старых на 6 новых пузырей:
     Невалид, Первый контакт, Общение, Обрыв,
     Завершен успешно, Завершен провал.

4. Новая таблица `crm_lead_pool` (собственность модуля `partner_crm`) —
   общий пул лидов из CRM, ожидающих подбор воркером.

5. Новая таблица `lead_evaluations` (собственность модуля `lead_evaluator`) —
   результат оценки закрытого диалога LLM-судьёй + извлечённые
   поля профиля лида.

6. Новые ключи в `settings` для partner_crm и lead_evaluator.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-17

"""
from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── dialogs: gender / from_crm / partner_crm_terminal_reason ──────
    op.execute("""
        ALTER TABLE dialogs
        ADD COLUMN gender TEXT,
        ADD COLUMN from_crm BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN partner_crm_terminal_reason TEXT,
        ADD CONSTRAINT dialogs_gender_chk
            CHECK (gender IS NULL OR gender IN ('female', 'male')),
        ADD CONSTRAINT dialogs_terminal_reason_chk
            CHECK (partner_crm_terminal_reason IS NULL
                   OR partner_crm_terminal_reason IN (
                        'ready_for_transfer', 'invalid', 'no_response'));
    """)
    # Частичный индекс — почти всегда NULL у ручных диалогов, его не индексируем.
    op.execute("""
        CREATE INDEX dialogs_from_crm_idx
            ON dialogs (id) WHERE from_crm = TRUE;
    """)

    # ── accounts: lead_source / target_gender ─────────────────────────
    op.execute("""
        ALTER TABLE accounts
        ADD COLUMN lead_source TEXT NOT NULL DEFAULT 'manual',
        ADD COLUMN target_gender TEXT NOT NULL DEFAULT 'any',
        ADD CONSTRAINT accounts_lead_source_chk
            CHECK (lead_source IN ('manual', 'partner_crm')),
        ADD CONSTRAINT accounts_target_gender_chk
            CHECK (target_gender IN ('any', 'female', 'male'));
    """)

    # ── dialog_statuses.partner_terminal_status + переезд сидов ───────
    op.execute("""
        ALTER TABLE dialog_statuses
        ADD COLUMN partner_terminal_status TEXT,
        ADD CONSTRAINT dialog_statuses_partner_terminal_chk
            CHECK (partner_terminal_status IS NULL
                   OR partner_terminal_status IN (
                        'ready_for_transfer', 'invalid', 'no_response'));
    """)

    # Переезд сидов: если каталог пустой ИЛИ совпадает с дефолтом из 0009 —
    # заменяем на новые 6 пузырей. Иначе оставляем (оператор уже редактировал).
    op.execute("""
        DO $$
        DECLARE
            current_count INTEGER;
            is_default_old BOOLEAN;
            should_seed BOOLEAN;
        BEGIN
            SELECT COUNT(*) INTO current_count FROM dialog_statuses;

            -- "Старый дефолт" = ровно 4 строки из миграции 0009.
            SELECT (current_count = 4 AND EXISTS (
                SELECT 1 FROM dialog_statuses
                WHERE label = 'Общаемся' AND id = 1
            ) AND EXISTS (
                SELECT 1 FROM dialog_statuses
                WHERE label = 'Ждём ответ' AND id = 2
            ) AND EXISTS (
                SELECT 1 FROM dialog_statuses
                WHERE label = 'Готово' AND id = 3
            ) AND EXISTS (
                SELECT 1 FROM dialog_statuses
                WHERE label = 'Провал' AND id = 4
            )) INTO is_default_old;

            -- Сидим если пусто или дефолт. Если оператор накастомил —
            -- не трогаем, ему нужно вручную подправить терминальные пузыри.
            should_seed := (current_count = 0) OR is_default_old;

            IF should_seed THEN
                UPDATE dialogs SET status_id = NULL WHERE status_id IS NOT NULL;
                DELETE FROM dialog_statuses;

                INSERT INTO dialog_statuses
                    (id, label, color, sort_order, partner_terminal_status)
                VALUES
                    (1, 'Невалид',          '#dc2626', 1, 'invalid'),
                    (2, 'Первый контакт',   '#94a3b8', 2, NULL),
                    (3, 'Общение',          '#3b82f6', 3, NULL),
                    (4, 'Обрыв',            '#f59e0b', 4, NULL),
                    (5, 'Завершен успешно', '#22c55e', 5, 'ready_for_transfer'),
                    (6, 'Завершен провал',  '#ef4444', 6, 'no_response');

                PERFORM setval('dialog_statuses_id_seq', 6, true);
            END IF;
        END $$;
    """)

    # ── crm_lead_pool ─────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE crm_lead_pool (
            id          SERIAL PRIMARY KEY,
            username    TEXT NOT NULL UNIQUE,
            gender      TEXT,
            fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT crm_lead_pool_gender_chk
                CHECK (gender IS NULL OR gender IN ('female', 'male'))
        );
        CREATE INDEX crm_lead_pool_gender_fetched_idx
            ON crm_lead_pool (gender, fetched_at);
    """)

    # ── lead_evaluations ──────────────────────────────────────────────
    op.execute("""
        CREATE TABLE lead_evaluations (
            id                    SERIAL PRIMARY KEY,
            dialog_id             INTEGER NOT NULL UNIQUE
                                  REFERENCES dialogs(id) ON DELETE CASCADE,
            verdict               TEXT NOT NULL,
            extracted_name        TEXT,
            extracted_age         SMALLINT,
            extracted_city        TEXT,
            extracted_workplace   TEXT,
            extracted_interests   TEXT,
            raw_response          TEXT,
            evaluator_model       TEXT,
            parse_succeeded       BOOLEAN NOT NULL DEFAULT TRUE,
            evaluated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT lead_evaluations_verdict_chk
                CHECK (verdict IN ('success', 'archive')),
            CONSTRAINT lead_evaluations_age_chk
                CHECK (extracted_age IS NULL
                       OR (extracted_age BETWEEN 10 AND 120))
        );
    """)

    # ── новые ключи в settings ────────────────────────────────────────
    op.execute("""
        INSERT INTO settings (key, value, description) VALUES
            ('partner_crm.poll_interval_sec',         '60',
                'Как часто сторож пула смотрит размер crm_lead_pool'),
            ('partner_crm.pool_low_threshold',        '10',
                'Ниже какого размера пула запрашиваем дозалив из CRM'),
            ('partner_crm.refill_batch_size',         '50',
                'Сколько лидов запрашиваем за один request-new'),
            ('lead_evaluator.silence_check_interval_sec', '300',
                'Как часто проверяем таймеры тишины'),
            ('lead_evaluator.silence_to_obryv_hours', '4',
                'Часов тишины после ответа лида до перехода в Обрыв'),
            ('lead_evaluator.silence_to_close_hours', '48',
                'Часов от Первого контакта/Обрыва до закрытия как no_response'),
            ('lead_evaluator.parse_retries',          '1',
                'Сколько раз ретраим LLM при непарсящемся ответе');
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM settings
        WHERE key IN (
            'partner_crm.poll_interval_sec',
            'partner_crm.pool_low_threshold',
            'partner_crm.refill_batch_size',
            'lead_evaluator.silence_check_interval_sec',
            'lead_evaluator.silence_to_obryv_hours',
            'lead_evaluator.silence_to_close_hours',
            'lead_evaluator.parse_retries'
        );
    """)
    op.execute("DROP TABLE IF EXISTS lead_evaluations;")
    op.execute("DROP TABLE IF EXISTS crm_lead_pool;")

    # Откат сидов dialog_statuses — возвращаем 4 старых, обнуляя status_id
    # на диалогах (по аналогии с upgrade — кастомные пузыри не сохраним).
    op.execute("""
        DO $$
        DECLARE
            current_count INTEGER;
            is_default_new BOOLEAN;
        BEGIN
            SELECT COUNT(*) INTO current_count FROM dialog_statuses;
            SELECT (current_count = 6 AND EXISTS (
                SELECT 1 FROM dialog_statuses
                WHERE label = 'Невалид' AND partner_terminal_status = 'invalid'
            )) INTO is_default_new;

            IF is_default_new THEN
                UPDATE dialogs SET status_id = NULL WHERE status_id IS NOT NULL;
                DELETE FROM dialog_statuses;
                INSERT INTO dialog_statuses (id, label, color, sort_order) VALUES
                    (1, 'Общаемся',   '#22c55e', 1),
                    (2, 'Ждём ответ', '#eab308', 2),
                    (3, 'Готово',     '#94a3b8', 3),
                    (4, 'Провал',     '#ef4444', 4);
                PERFORM setval('dialog_statuses_id_seq', 4, true);
            END IF;
        END $$;
    """)

    op.execute("""
        ALTER TABLE dialog_statuses
        DROP CONSTRAINT IF EXISTS dialog_statuses_partner_terminal_chk,
        DROP COLUMN IF EXISTS partner_terminal_status;
    """)

    op.execute("""
        ALTER TABLE accounts
        DROP CONSTRAINT IF EXISTS accounts_target_gender_chk,
        DROP CONSTRAINT IF EXISTS accounts_lead_source_chk,
        DROP COLUMN IF EXISTS target_gender,
        DROP COLUMN IF EXISTS lead_source;
    """)

    op.execute("DROP INDEX IF EXISTS dialogs_from_crm_idx;")
    op.execute("""
        ALTER TABLE dialogs
        DROP CONSTRAINT IF EXISTS dialogs_terminal_reason_chk,
        DROP CONSTRAINT IF EXISTS dialogs_gender_chk,
        DROP COLUMN IF EXISTS partner_crm_terminal_reason,
        DROP COLUMN IF EXISTS from_crm,
        DROP COLUMN IF EXISTS gender;
    """)
