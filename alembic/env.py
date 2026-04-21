"""
Alembic env.py.

URL к PostgreSQL берём из core.config (чтобы не дублировать в alembic.ini).
Таблицы описываем raw SQL в самих миграциях — SQLAlchemy-моделей не ведём,
поэтому target_metadata = None и --autogenerate нам не нужен
(новые миграции пишем руками).
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Добавляем корень проекта в sys.path, чтобы импортировать core.config
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import settings  # noqa: E402

config = context.config
config.set_main_option("sqlalchemy.url", settings.postgres_dsn)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ORM не используем — миграции пишем руками через op.execute()
target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
