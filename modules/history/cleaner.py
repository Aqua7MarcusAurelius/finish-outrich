"""
Чистильщик файлов MinIO.

Раз в cleaner.interval_hours ищет в media устаревшие файлы (старше
cleaner.file_ttl_days дней), пачкой до cleaner.batch_size удаляет их
из MinIO и проставляет storage_key=NULL + file_deleted_at=NOW() в БД.
Метаданные, транскрипция и описание — навсегда остаются.

Файлы со статусом pending у транскрипции или описания не трогаются —
их ещё обрабатывают медиа-модули.

См. docs/history.md → "Чистильщик файлов".
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from core import bus, db
from core import minio as minio_mod
from core.events import EventType, Module, Status

log = logging.getLogger(__name__)

# Дефолты на случай если записи в settings нет (должна быть — создаётся миграцией 0001)
DEFAULT_INTERVAL_HOURS = 1.0
DEFAULT_BATCH_SIZE = 50
DEFAULT_TTL_DAYS = 3.0

# Минимальная пауза между прогонами — защита от случайно выставленного нуля
MIN_SLEEP_SECONDS = 60


class Cleaner:
    """
    Фоновая задача. Запускается как asyncio.Task в lifespan.
    """

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        log.info("Cleaner started")

        while not self._stop_event.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Cleaner iteration error")
                try:
                    await bus.publish(
                        module=Module.CLEANER,
                        type=EventType.SYSTEM_ERROR,
                        status=Status.ERROR,
                        data={"message": f"cleaner iteration error: {e}"},
                    )
                except Exception:
                    log.exception("Cleaner failed to publish system.error")

            # Пауза до следующего прогона
            interval_hours, _, _ = await self._get_settings()
            sleep_seconds = max(MIN_SLEEP_SECONDS, int(interval_hours * 3600))

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=sleep_seconds,
                )
                # stop_event установлен — выходим из цикла
                break
            except asyncio.TimeoutError:
                continue

        log.info("Cleaner stopped")

    async def stop(self) -> None:
        self._stop_event.set()

    # ─── Один прогон ──────────────────────────────────────────────

    async def _run_once(self) -> None:
        interval_hours, batch_size, ttl_days = await self._get_settings()
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=int(ttl_days * 86400))

        pool = db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, storage_key FROM media
                WHERE file_deleted_at IS NULL
                  AND storage_key IS NOT NULL
                  AND downloaded_at < $1
                  AND transcription_status != 'pending'
                  AND description_status != 'pending'
                ORDER BY downloaded_at
                LIMIT $2
                """,
                cutoff, batch_size,
            )

        if not rows:
            return

        # Пытаемся удалить каждый файл. Успешно удалённые собираем
        # отдельно — БД обновляется только по ним.
        deleted_ids: list[int] = []
        deleted_keys: list[str] = []
        for row in rows:
            key = row["storage_key"]
            try:
                await minio_mod.remove_object(key)
                deleted_ids.append(row["id"])
                deleted_keys.append(key)
            except Exception:
                log.exception(
                    "Cleaner: failed to remove %s from MinIO — retry next run",
                    key,
                )

        if not deleted_ids:
            # Все попытки упали — инфра-ошибка (скорее всего MinIO лёг)
            await bus.publish(
                module=Module.CLEANER,
                type=EventType.SYSTEM_ERROR,
                status=Status.ERROR,
                data={
                    "message": (
                        f"не удалось удалить ни одного из {len(rows)} "
                        f"файлов из MinIO"
                    ),
                    "expected_count": len(rows),
                },
            )
            return

        # Обновляем БД только для успешно удалённых
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE media SET
                    storage_key = NULL,
                    file_deleted_at = NOW()
                WHERE id = ANY($1::int[])
                """,
                deleted_ids,
            )

        await bus.publish(
            module=Module.CLEANER,
            type=EventType.FILE_CLEANED,
            status=Status.SUCCESS,
            data={
                "count": len(deleted_ids),
                "media_ids": deleted_ids,
                "ttl_days": ttl_days,
            },
        )
        log.info(
            "Cleaner: removed %d files from MinIO (ttl=%.2fd)",
            len(deleted_ids), ttl_days,
        )

    # ─── Чтение настроек ──────────────────────────────────────────

    async def _get_settings(self) -> tuple[float, int, float]:
        """Возвращает (interval_hours, batch_size, ttl_days) из таблицы settings."""
        pool = db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, value FROM settings
                WHERE key IN (
                    'cleaner.interval_hours',
                    'cleaner.batch_size',
                    'cleaner.file_ttl_days'
                )
                """
            )
        values = {r["key"]: r["value"] for r in rows}

        def _to_float(key: str, default: float) -> float:
            try:
                return float(values.get(key, default))
            except Exception:
                return default

        def _to_int(key: str, default: int) -> int:
            try:
                return int(values.get(key, default))
            except Exception:
                return default

        return (
            _to_float("cleaner.interval_hours", DEFAULT_INTERVAL_HOURS),
            _to_int("cleaner.batch_size", DEFAULT_BATCH_SIZE),
            _to_float("cleaner.file_ttl_days", DEFAULT_TTL_DAYS),
        )
