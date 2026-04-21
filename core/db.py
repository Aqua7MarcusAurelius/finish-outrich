"""
Подключение к PostgreSQL.

ORM не используем — SQL руками через asyncpg.
Единый пул на всё приложение, инициализируется в lifespan FastAPI.
"""
from __future__ import annotations

import asyncpg

from core.config import settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    """Создать пул подключений. Вызывается один раз на старте приложения."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=settings.POSTGRES_HOST,
            port=settings.POSTGRES_PORT,
            database=settings.POSTGRES_DB,
            user=settings.POSTGRES_USER,
            password=settings.POSTGRES_PASSWORD,
            min_size=2,
            max_size=10,
        )
    return _pool


async def close_pool() -> None:
    """Закрыть пул при остановке приложения."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Получить пул для использования в модулях."""
    if _pool is None:
        raise RuntimeError("DB pool is not initialized")
    return _pool


async def check_health() -> bool:
    """Проверка для /system/health."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False
