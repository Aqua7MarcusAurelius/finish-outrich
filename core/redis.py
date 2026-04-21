"""
Подключение к Redis.

Используем async-клиент из пакета redis (redis.asyncio).
Шину событий (Redis Streams) будем публиковать через этот же клиент —
соответствующий код добавится в core/bus.py на следующем этапе.
"""
from __future__ import annotations

import redis.asyncio as redis

from core.config import settings

_client: redis.Redis | None = None


async def init_client() -> redis.Redis:
    """Создать async-клиент Redis."""
    global _client
    if _client is None:
        _client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            decode_responses=False,  # Streams работают с bytes
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def get_client() -> redis.Redis:
    if _client is None:
        raise RuntimeError("Redis client is not initialized")
    return _client


async def check_health() -> bool:
    """Проверка для /system/health."""
    try:
        client = get_client()
        await client.ping()
        return True
    except Exception:
        return False
