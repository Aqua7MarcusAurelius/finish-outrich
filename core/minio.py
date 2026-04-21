"""
Подключение к MinIO.

Официальный minio-py — синхронный, поэтому оборачиваем вызовы в
asyncio.to_thread() чтобы не блокировать event loop.
На старте приложения создаём bucket если его ещё нет.
"""
from __future__ import annotations

import asyncio

from minio import Minio

from core.config import settings

_client: Minio | None = None


def _build_client() -> Minio:
    return Minio(
        endpoint=settings.minio_endpoint,
        access_key=settings.MINIO_ROOT_USER,
        secret_key=settings.MINIO_ROOT_PASSWORD,
        secure=False,  # внутри Docker-сети HTTP
    )


async def init_client() -> Minio:
    """Поднять клиент и создать bucket если его нет."""
    global _client
    if _client is None:
        _client = await asyncio.to_thread(_build_client)
        await ensure_bucket()
    return _client


async def ensure_bucket() -> None:
    if _client is None:
        raise RuntimeError("MinIO client is not initialized")
    client = _client
    bucket = settings.MINIO_BUCKET

    def _ensure() -> None:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)

    await asyncio.to_thread(_ensure)


def get_client() -> Minio:
    if _client is None:
        raise RuntimeError("MinIO client is not initialized")
    return _client


async def check_health() -> bool:
    """Проверка для /system/health."""
    try:
        client = get_client()
        await asyncio.to_thread(client.list_buckets)
        return True
    except Exception:
        return False
