"""
Модуль транскрибации.

Слушает шину через consumer group "transcription-worker":
  - message.saved             → для каждого media типа voice/audio/video/video_note
                                скачиваем из MinIO, конвертим в mp3 через ffmpeg,
                                отправляем в OpenRouter и публикуем transcription.done
  - media.reprocess.requested → то же самое, но media_id берём из payload и
                                storage_key/type подтягиваем из БД
  - (остальные)               → молча ack'аются

В БД ничего не пишет — результат в шину, history.service сам обновит media.
См. docs/transcription.md.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from core import bus, db
from core import minio as minio_mod
from core.events import EventType, Module, Status
from core.openrouter import OpenRouterError, transcribe_audio

from .ffmpeg import FfmpegError, to_mp3

log = logging.getLogger(__name__)

TRANSCRIPTION_GROUP = "transcription-worker"
TRANSCRIPTION_CONSUMER = "transcription-worker-1"

# Типы media которые умеем транскрибировать.
TRANSCRIBE_TYPES = {"voice", "audio", "video", "video_note"}

# Дефолт на случай отсутствия настройки в БД.
DEFAULT_RETRIES = 1


async def _get_retries() -> int:
    """Прочитать settings.transcription.retries из БД."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT value FROM settings WHERE key='transcription.retries'"
        )
    try:
        return int(val) if val is not None else DEFAULT_RETRIES
    except Exception:
        return DEFAULT_RETRIES


class TranscriptionService:
    """
    Consumer-loop модуля транскрибации. Живёт как фоновая задача в lifespan.
    """

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        await bus.ensure_group(TRANSCRIPTION_GROUP)
        log.info("TranscriptionService loop started")

        while not self._stop_event.is_set():
            try:
                batch = await bus.read_group(
                    TRANSCRIPTION_GROUP, TRANSCRIPTION_CONSUMER,
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
                            "transcription: failed to handle event %s (type=%s)",
                            event.get("id"), event.get("type"),
                        )
                        # Не ack'аем — переобработка в следующем прогоне.

                if ack_ids:
                    await bus.ack_group(TRANSCRIPTION_GROUP, ack_ids)

            except asyncio.CancelledError:
                log.info("TranscriptionService loop cancelled")
                raise
            except Exception:
                log.exception("TranscriptionService loop error")
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
        # Остальные — молча игнорируются.

    # ─── message.saved ────────────────────────────────────────────

    async def _on_message_saved(self, event: dict) -> None:
        data = event.get("data") or {}
        account_id = event.get("account_id")
        parent_id = event.get("id")
        media_list: list[dict] = data.get("media") or []

        # Быстрый фильтр по флагам — если аудио/видео не было, выходим.
        if not (data.get("has_audio") or data.get("has_video")):
            return

        for media in media_list:
            mtype = media.get("type")
            if mtype not in TRANSCRIBE_TYPES:
                continue
            await self._process_media(
                account_id=account_id,
                parent_id=parent_id,
                media_id=media.get("media_id"),
                media_type=mtype,
                storage_key=media.get("storage_key"),
            )

    # ─── media.reprocess.requested ────────────────────────────────

    async def _on_media_reprocess(self, event: dict) -> None:
        """
        Подхватываем только запросы на транскрибацию (описание — чужая епархия).
        storage_key/type берём из БД — в payload их может не быть.
        """
        data = event.get("data") or {}
        if data.get("field") != "transcription":
            return

        media_id = data.get("media_id")
        if media_id is None:
            log.warning("reprocess: no media_id in %s", event.get("id"))
            return

        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT type, storage_key FROM media WHERE id = $1",
                media_id,
            )
        if row is None:
            log.warning("reprocess: media %s not found", media_id)
            return

        mtype = row["type"]
        if mtype not in TRANSCRIBE_TYPES:
            log.info("reprocess: media %s type=%s not transcribable", media_id, mtype)
            return

        await self._process_media(
            account_id=event.get("account_id"),
            parent_id=event.get("id"),
            media_id=media_id,
            media_type=mtype,
            storage_key=row["storage_key"],
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
    ) -> None:
        if media_id is None:
            log.warning("process_media: media_id is None, skip")
            return

        # Файл уже удалён чистильщиком (или не скачался) — это ошибка.
        if not storage_key:
            log.info("transcription: media %s has no storage_key", media_id)
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

        # 1) transcription.started
        started = await bus.publish(
            module=Module.TRANSCRIPTION,
            type=EventType.TRANSCRIPTION_STARTED,
            status=Status.IN_PROGRESS,
            parent_id=parent_id,
            account_id=account_id,
            data={"media_id": media_id, "media_type": media_type},
        )
        started_id = started["id"]

        # 2) скачиваем из MinIO
        try:
            raw = await minio_mod.get_object(storage_key)
        except Exception as e:
            log.exception("minio.get_object failed for %s", storage_key)
            await bus.publish(
                module=Module.TRANSCRIPTION,
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

        # 3) конвертим в mp3 (ogg/opus, mp4, что угодно → mp3)
        try:
            mp3 = await to_mp3(raw)
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

        # 4) OpenRouter + ретраи
        retries = await _get_retries()
        text, status_str, error = await self._call_with_retries(mp3, retries)

        # 5) transcription.done
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

    async def _call_with_retries(
        self, mp3: bytes, retries: int,
    ) -> tuple[str, str, str | None]:
        """
        Вызов OpenRouter с политикой из docs/transcription.md:
          • При ошибке — до `retries` повторных попыток (дефолт 1).
          • Если после всех попыток всё равно ошибка — вернём ("", "failed", err).
          • Если пришёл пустой текст — один повтор; потом done (пустой — валидный результат).

        Возвращает (text, status_str, error).
        status_str: "done" | "failed"
        """
        last_error: str | None = None
        attempts = max(1, 1 + retries)

        for attempt in range(attempts):
            try:
                text = await transcribe_audio(mp3)
            except OpenRouterError as e:
                last_error = str(e)
                log.warning(
                    "transcription: OpenRouter error attempt=%d/%d: %s",
                    attempt + 1, attempts, e,
                )
                continue

            # Получили валидный ответ (возможно пустой).
            if text:
                return text, "done", None

            # Пустой результат — одна повторная попытка.
            try:
                retry_text = await transcribe_audio(mp3)
            except OpenRouterError as e:
                log.warning("transcription: retry-on-empty OpenRouter error: %s", e)
                # Первый ответ был валидным (пустым), ошибка на ретрае — фиксируем пустой done.
                return "", "done", None
            # Пустой или нет — считаем финальным результатом.
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
            module=Module.TRANSCRIPTION,
            type=EventType.TRANSCRIPTION_DONE,
            status=event_status,
            parent_id=parent_id,
            account_id=account_id,
            data=payload,
        )