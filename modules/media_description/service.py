"""
Модуль описания медиа.

Слушает шину через consumer group "description-worker":
  - message.saved             → для каждого media визуального типа (photo/
                                sticker/gif/video/video_note) или document —
                                скачиваем из MinIO, при необходимости нарезаем
                                кадры, отправляем в OpenRouter и публикуем
                                description.done
  - media.reprocess.requested → то же самое, с field=description; type/storage_key
                                берём из БД
  - (остальные)               → молча ack'аются

В БД ничего не пишет — результат в шину, history.service сам обновит
media.description и description_status.

См. docs/media_description.md.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from core import bus, db
from core import minio as minio_mod
from core.events import EventType, Module, Status
from core.openrouter import OpenRouterError, describe_document, describe_images

from .ffmpeg import FfmpegError, NoFramesError, extract_frames

log = logging.getLogger(__name__)

DESCRIPTION_GROUP = "description-worker"
DESCRIPTION_CONSUMER = "description-worker-1"

# Типы media и как их обрабатываем.
STATIC_IMAGE_TYPES = {"photo", "sticker"}            # одна картинка как есть
FRAME_TYPES = {"gif", "video", "video_note"}          # нарезка кадров
DOCUMENT_TYPES = {"document"}                         # файл в модель
DESCRIBE_TYPES = STATIC_IMAGE_TYPES | FRAME_TYPES | DOCUMENT_TYPES

# Дефолты на случай отсутствия настроек в БД.
DEFAULT_RETRIES = 1
DEFAULT_FRAMES_COUNT = 5


async def _get_int_setting(key: str, default: int) -> int:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT value FROM settings WHERE key=$1", key,
        )
    try:
        return int(val) if val is not None else default
    except Exception:
        return default


async def _get_retries() -> int:
    return await _get_int_setting("description.retries", DEFAULT_RETRIES)


async def _get_frames_count() -> int:
    n = await _get_int_setting("description.frames_count", DEFAULT_FRAMES_COUNT)
    return max(1, n)


def _image_format_from_mime(mime_type: str | None) -> str:
    """image/webp → webp, image/png → png, всё остальное → jpeg."""
    if not mime_type or "/" not in mime_type:
        return "jpeg"
    sub = mime_type.split("/", 1)[1].lower()
    if sub in ("png", "webp", "gif", "jpeg"):
        return "jpeg" if sub == "gif" else sub  # GIF как static-image не придёт, но на всякий
    return "jpeg"


def _filename_from_mime(mime_type: str | None) -> str:
    """Сгенерировать имя файла для OpenRouter. Реальное имя не важно — Gemini
    смотрит на MIME из data-URL. Но поле filename обязательное по спеке."""
    if mime_type == "application/pdf":
        return "document.pdf"
    # Грубая эвристика; в любом случае это только метка.
    if mime_type and "/" in mime_type:
        ext = mime_type.split("/", 1)[1].split(";")[0]
        return f"document.{ext}"
    return "document.bin"


class DescriptionService:
    """Consumer-loop модуля описания. Живёт как фоновая задача в lifespan."""

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        await bus.ensure_group(DESCRIPTION_GROUP)
        log.info("DescriptionService loop started")

        while not self._stop_event.is_set():
            try:
                batch = await bus.read_group(
                    DESCRIPTION_GROUP, DESCRIPTION_CONSUMER,
                    count=20, block_ms=5000,
                )
                if not batch:
                    continue

                ack_ids: list[str] = []
                for stream_id, event in batch:
                    try:
                        await self._handle(event)
                        ack_ids.append(stream_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception(
                            "description: failed to handle event %s (type=%s)",
                            event.get("id"), event.get("type"),
                        )
                        # Не ack'аем — переобработка в следующем прогоне.

                if ack_ids:
                    await bus.ack_group(DESCRIPTION_GROUP, ack_ids)

            except asyncio.CancelledError:
                log.info("DescriptionService loop cancelled")
                raise
            except Exception:
                log.exception("DescriptionService loop error")
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stop_event.set()

    # ─── Dispatcher ───────────────────────────────────────────────

    async def _handle(self, event: dict) -> None:
        et = event.get("type")
        if et == EventType.MESSAGE_SAVED:
            await self._on_message_saved(event)
        elif et == EventType.MEDIA_REPROCESS_REQUESTED:
            await self._on_media_reprocess(event)

    # ─── message.saved ────────────────────────────────────────────

    async def _on_message_saved(self, event: dict) -> None:
        data = event.get("data") or {}
        account_id = event.get("account_id")
        parent_id = event.get("id")
        media_list: list[dict] = data.get("media") or []

        # Быстрый фильтр по флагам — если нечего описывать, выходим.
        if not (
            data.get("has_image") or data.get("has_video") or data.get("has_document")
        ):
            return

        for media in media_list:
            mtype = media.get("type")
            if mtype not in DESCRIBE_TYPES:
                continue
            await self._process_media(
                account_id=account_id,
                parent_id=parent_id,
                media_id=media.get("media_id"),
                media_type=mtype,
                storage_key=media.get("storage_key"),
                mime_type=media.get("mime_type"),
            )

    # ─── media.reprocess.requested ────────────────────────────────

    async def _on_media_reprocess(self, event: dict) -> None:
        data = event.get("data") or {}
        if data.get("field") != "description":
            return

        media_id = data.get("media_id")
        if media_id is None:
            log.warning("reprocess: no media_id in %s", event.get("id"))
            return

        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT type, storage_key, mime_type FROM media WHERE id = $1",
                media_id,
            )
        if row is None:
            log.warning("reprocess: media %s not found", media_id)
            return

        mtype = row["type"]
        if mtype not in DESCRIBE_TYPES:
            log.info("reprocess: media %s type=%s not describable", media_id, mtype)
            return

        await self._process_media(
            account_id=event.get("account_id"),
            parent_id=event.get("id"),
            media_id=media_id,
            media_type=mtype,
            storage_key=row["storage_key"],
            mime_type=row["mime_type"],
        )

    # ─── Основной флоу одного media ───────────────────────────────

    async def _process_media(
        self,
        *,
        account_id: int | None,
        parent_id: str | None,
        media_id: int | None,
        media_type: str,
        storage_key: str | None,
        mime_type: str | None,
    ) -> None:
        if media_id is None:
            log.warning("process_media: media_id is None, skip")
            return

        if not storage_key:
            log.info("description: media %s has no storage_key", media_id)
            await self._publish_done(
                account_id=account_id,
                parent_id=parent_id,
                media_id=media_id,
                text="",
                status_str="failed",
                error="file_not_available",
                event_status=Status.ERROR,
            )
            return

        # 1) description.started
        started = await bus.publish(
            module=Module.DESCRIPTION,
            type=EventType.DESCRIPTION_STARTED,
            status=Status.IN_PROGRESS,
            parent_id=parent_id,
            account_id=account_id,
            data={"media_id": media_id, "media_type": media_type},
        )
        started_id = started["id"]

        # 2) скачиваем файл
        try:
            raw = await minio_mod.get_object(storage_key)
        except Exception as e:
            log.exception("minio.get_object failed for %s", storage_key)
            await bus.publish(
                module=Module.DESCRIPTION,
                type=EventType.SYSTEM_ERROR,
                status=Status.ERROR,
                parent_id=started_id,
                account_id=account_id,
                data={"message": f"minio get: {e}", "media_id": media_id},
            )
            await self._publish_done(
                account_id=account_id,
                parent_id=started_id,
                media_id=media_id,
                text="",
                status_str="failed",
                error=f"minio: {e}",
                event_status=Status.ERROR,
            )
            return

        # 3) маршрут по типу: собираем то, что будем отправлять в модель
        retries = await _get_retries()

        if media_type in DOCUMENT_TYPES:
            text, status_str, error = await self._describe_document_retries(
                doc_bytes=raw,
                mime_type=mime_type or "application/octet-stream",
                retries=retries,
            )
        elif media_type in STATIC_IMAGE_TYPES:
            fmt = _image_format_from_mime(mime_type)
            text, status_str, error = await self._describe_images_retries(
                images=[raw], image_format=fmt, retries=retries,
            )
        else:
            # FRAME_TYPES: gif/video/video_note — нарезаем кадры
            try:
                frames_count = await _get_frames_count()
                frames = await extract_frames(raw, frames_count)
            except NoFramesError as e:
                log.info("description: media=%s — no frames (%s)", media_id, e)
                await self._publish_done(
                    account_id=account_id,
                    parent_id=started_id,
                    media_id=media_id,
                    text="",
                    status_str="done",
                    error=None,
                    event_status=Status.SUCCESS,
                )
                return
            except FfmpegError as e:
                log.warning("ffmpeg failed for media %s: %s", media_id, e)
                await self._publish_done(
                    account_id=account_id,
                    parent_id=started_id,
                    media_id=media_id,
                    text="",
                    status_str="failed",
                    error=f"ffmpeg: {e}",
                    event_status=Status.ERROR,
                )
                return

            text, status_str, error = await self._describe_images_retries(
                images=frames, image_format="jpeg", retries=retries,
            )

        # 4) description.done
        await self._publish_done(
            account_id=account_id,
            parent_id=started_id,
            media_id=media_id,
            text=text,
            status_str=status_str,
            error=error,
            event_status=(
                Status.SUCCESS if status_str == "done" else Status.ERROR
            ),
        )

    # ─── Ретраи (симметрично transcription._call_with_retries) ────

    async def _describe_images_retries(
        self, *, images: list[bytes], image_format: str, retries: int,
    ) -> tuple[str, str, str | None]:
        return await self._retry_policy(
            call=lambda: describe_images(images, image_format=image_format),
            retries=retries,
        )

    async def _describe_document_retries(
        self, *, doc_bytes: bytes, mime_type: str, retries: int,
    ) -> tuple[str, str, str | None]:
        filename = _filename_from_mime(mime_type)
        return await self._retry_policy(
            call=lambda: describe_document(
                doc_bytes, filename=filename, mime_type=mime_type,
            ),
            retries=retries,
        )

    async def _retry_policy(
        self, *, call, retries: int,
    ) -> tuple[str, str, str | None]:
        """
        Единая политика (по docs/media_description.md):
          • При ошибке — до `retries` повторных попыток.
          • Если после всех попыток всё равно ошибка → ("", "failed", err).
          • Если пришёл пустой ответ — один повтор; что бы ни пришло дальше —
            фиксируем как "done" (пустое — валидный результат).
        """
        last_error: str | None = None
        attempts = max(1, 1 + retries)

        for attempt in range(attempts):
            try:
                text = await call()
            except OpenRouterError as e:
                last_error = str(e)
                log.warning(
                    "description: OpenRouter error attempt=%d/%d: %s",
                    attempt + 1, attempts, e,
                )
                continue

            if text:
                return text, "done", None

            # Пустой — одна повторная попытка.
            try:
                retry_text = await call()
            except OpenRouterError as e:
                log.warning("description: retry-on-empty OpenRouter error: %s", e)
                return "", "done", None
            return retry_text, "done", None

        return "", "failed", last_error

    async def _publish_done(
        self,
        *,
        account_id: int | None,
        parent_id: str | None,
        media_id: int,
        text: str,
        status_str: str,
        error: str | None,
        event_status: str,
    ) -> None:
        payload: dict[str, Any] = {
            "media_id": media_id,
            "text": text,
            "status": status_str,
        }
        if error:
            payload["error"] = error

        await bus.publish(
            module=Module.DESCRIPTION,
            type=EventType.DESCRIPTION_DONE,
            status=event_status,
            parent_id=parent_id,
            account_id=account_id,
            data=payload,
        )
