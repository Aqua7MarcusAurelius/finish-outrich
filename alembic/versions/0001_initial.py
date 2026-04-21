"""initial schema

Создаёт все таблицы проекта и заполняет settings дефолтными значениями.
Структура и обоснования — см. docs/database_schema.md.

Revision ID: 0001
Revises:
Create Date: 2026-04-21

"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─────────────────────────────────────────────────────────────────
    # accounts — наши Telegram-аккаунты
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE accounts (
            id              SERIAL PRIMARY KEY,
            name            TEXT NOT NULL,
            phone           TEXT NOT NULL UNIQUE,
            session_data    BYTEA,
            proxy_primary   TEXT NOT NULL,
            proxy_fallback  TEXT NOT NULL,
            is_active       BOOLEAN NOT NULL DEFAULT FALSE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)

    # ─────────────────────────────────────────────────────────────────
    # dialogs — с кем мы общаемся (один собеседник одного аккаунта)
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE dialogs (
            id                  SERIAL PRIMARY KEY,
            account_id          INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            telegram_user_id    BIGINT NOT NULL,
            type                TEXT NOT NULL DEFAULT 'private',
            username            TEXT,
            first_name          TEXT,
            last_name           TEXT,
            phone               TEXT,
            birthday            DATE,
            bio                 TEXT,
            is_contact          BOOLEAN NOT NULL DEFAULT FALSE,
            contact_first_name  TEXT,
            contact_last_name   TEXT,
            is_bot              BOOLEAN NOT NULL DEFAULT FALSE,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT dialogs_account_user_uniq UNIQUE (account_id, telegram_user_id)
        );
    """)

    # ─────────────────────────────────────────────────────────────────
    # messages — сообщения
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE messages (
            id                      SERIAL PRIMARY KEY,
            dialog_id               INTEGER NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
            telegram_message_id     BIGINT NOT NULL,
            is_outgoing             BOOLEAN NOT NULL,
            type                    TEXT NOT NULL DEFAULT 'regular',
            date                    TIMESTAMPTZ NOT NULL,
            text                    TEXT,
            reply_to_message_id     INTEGER REFERENCES messages(id) ON DELETE SET NULL,
            forward_from_user_id    BIGINT,
            forward_from_username   TEXT,
            forward_from_name       TEXT,
            forward_from_chat_id    BIGINT,
            forward_date            TIMESTAMPTZ,
            media_group_id          BIGINT,
            edited_at               TIMESTAMPTZ,
            deleted_at              TIMESTAMPTZ,
            CONSTRAINT messages_dialog_tgmsg_uniq UNIQUE (dialog_id, telegram_message_id)
        );
    """)
    # Все сообщения диалога по порядку
    op.execute("CREATE INDEX messages_dialog_date_idx ON messages (dialog_id, date);")

    # ─────────────────────────────────────────────────────────────────
    # media — вложения
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE media (
            id                      SERIAL PRIMARY KEY,
            message_id              INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            type                    TEXT NOT NULL,
            file_name               TEXT,
            telegram_file_id        TEXT,
            storage_key             TEXT,
            mime_type               TEXT,
            file_size               BIGINT,
            duration                INTEGER,
            width                   INTEGER,
            height                  INTEGER,
            transcription           TEXT,
            transcription_status    TEXT NOT NULL DEFAULT 'none',
            description             TEXT,
            description_status      TEXT NOT NULL DEFAULT 'none',
            downloaded_at           TIMESTAMPTZ,
            file_deleted_at         TIMESTAMPTZ
        );
    """)
    op.execute("CREATE INDEX media_storage_key_idx ON media (storage_key);")
    # Чистильщик ищет по downloaded_at только среди ещё живых файлов
    op.execute("""
        CREATE INDEX media_cleaner_idx ON media (downloaded_at)
        WHERE file_deleted_at IS NULL;
    """)

    # ─────────────────────────────────────────────────────────────────
    # reactions — реакции на сообщения
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE reactions (
            id                  SERIAL PRIMARY KEY,
            message_id          INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            is_outgoing         BOOLEAN NOT NULL,
            emoji               TEXT,
            custom_emoji_id     TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            removed_at          TIMESTAMPTZ,
            CONSTRAINT reactions_emoji_chk CHECK (
                (emoji IS NOT NULL AND custom_emoji_id IS NULL)
                OR (emoji IS NULL AND custom_emoji_id IS NOT NULL)
            )
        );
    """)
    op.execute("CREATE INDEX reactions_message_idx ON reactions (message_id);")

    # ─────────────────────────────────────────────────────────────────
    # message_edits — история редактирования текста
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE message_edits (
            id          SERIAL PRIMARY KEY,
            message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            old_text    TEXT,
            edited_at   TIMESTAMPTZ NOT NULL
        );
    """)

    # ─────────────────────────────────────────────────────────────────
    # settings — настройки поведения модулей
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            description TEXT,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    # Дефолтные значения — см. configuration.md
    op.execute("""
        INSERT INTO settings (key, value, description) VALUES
            ('cleaner.interval_hours',   '1',   'Как часто запускается чистильщик (в часах)'),
            ('cleaner.batch_size',       '50',  'Сколько файлов удаляет за один прогон'),
            ('cleaner.file_ttl_days',    '3',   'Сколько дней хранятся файлы в MinIO'),
            ('transcription.retries',    '1',   'Сколько повторных попыток при ошибке OpenRouter'),
            ('description.retries',      '1',   'Сколько повторных попыток при ошибке OpenRouter'),
            ('description.frames_count', '5',   'Сколько кадров нарезает FFmpeg из видео / GIF / кружков'),
            ('history_sync.chunk_size',  '100', 'Сколько сообщений запрашивает нагон за один раз');
    """)

    # ─────────────────────────────────────────────────────────────────
    # events_archive — архив событий шины
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE events_archive (
            id          TEXT PRIMARY KEY,
            parent_id   TEXT,
            time        TIMESTAMPTZ NOT NULL,
            account_id  INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            module      TEXT NOT NULL,
            type        TEXT NOT NULL,
            status      TEXT NOT NULL,
            data        JSONB NOT NULL DEFAULT '{}'::jsonb
        );
    """)
    op.execute("CREATE INDEX events_time_idx          ON events_archive (time DESC);")
    op.execute("CREATE INDEX events_account_time_idx  ON events_archive (account_id, time DESC);")
    op.execute("CREATE INDEX events_module_time_idx   ON events_archive (module, time DESC);")
    op.execute("CREATE INDEX events_type_time_idx     ON events_archive (type, time DESC);")
    op.execute("CREATE INDEX events_parent_idx        ON events_archive (parent_id);")
    op.execute("""
        CREATE INDEX events_error_idx ON events_archive (status, time DESC)
        WHERE status = 'error';
    """)


def downgrade() -> None:
    # Порядок обратный созданию из-за FK
    op.execute("DROP TABLE IF EXISTS events_archive;")
    op.execute("DROP TABLE IF EXISTS settings;")
    op.execute("DROP TABLE IF EXISTS message_edits;")
    op.execute("DROP TABLE IF EXISTS reactions;")
    op.execute("DROP TABLE IF EXISTS media;")
    op.execute("DROP TABLE IF EXISTS messages;")
    op.execute("DROP TABLE IF EXISTS dialogs;")
    op.execute("DROP TABLE IF EXISTS accounts;")
