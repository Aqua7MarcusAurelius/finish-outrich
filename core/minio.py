"""
Подключение к MinIO.

Официальный minio-py — синхронный, поэтому оборачиваем вызовы в
asyncio.to_thread() чтобы не блокировать event loop.
На старте приложения создаём bucket если его ещё нет.
"""
from __future__ import annotations

import asyncio
import logging

from minio import Minio
from minio.deleteobjects import DeleteObject

from core.config import settings

log = logging.getLogger(__name__)

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


# ─────────────────────────────────────────────────────────────────────
# Операции с объектами
# ─────────────────────────────────────────────────────────────────────

async def remove_object(storage_key: str) -> None:
    """Удалить один объект из bucket-а. Ошибки пробрасываются наружу."""
    client = get_client()
    bucket = settings.MINIO_BUCKET
    await asyncio.to_thread(client.remove_object, bucket, storage_key)


async def remove_objects(storage_keys: list[str]) -> int:
    """
    Удалить пачку объектов. Возвращает количество успешно удалённых.
    Используется чистильщиком и при удалении аккаунта.
    """
    if not storage_keys:
        return 0
    client = get_client()
    bucket = settings.MINIO_BUCKET

    def _remove() -> int:
        errors = list(client.remove_objects(
            bucket, (DeleteObject(k) for k in storage_keys),
        ))
        for err in errors:
            log.warning("minio remove error: %s", err)
        return len(storage_keys) - len(errors)

    return await asyncio.to_thread(_remove)