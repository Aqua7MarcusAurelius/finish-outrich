"""
Endpoints модуля медиа:

    GET   /media/{id}               — метаданные + file_available
    GET   /media/{id}/file           — стрим файла из MinIO
    POST  /media/{id}/retranscribe   — сбросить статус + media.reprocess.requested
    POST  /media/{id}/redescribe     — то же для описания

См. docs/api.md. Слушатели `media.reprocess.requested` живут в
TranscriptionService (field=transcription) и DescriptionService
(field=description) — этап 5/6.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from core import bus, db
from core import minio as minio_mod
from core.events import EventType, Module, Status

router = APIRouter(tags=["media"])


# ─── Типы, которые можно (ре)транскрибировать / описать ──────────────
# Те же константы, что в сервисах — но дублируем здесь, чтобы не тащить
# зависимость на модули: endpoint должен валидировать сам.

_TRANSCRIBE_TYPES = {"voice", "audio", "video", "video_note"}
_DESCRIBE_TYPES = {
    "photo", "sticker", "gif", "video", "video_note", "document",
}


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _media_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "message_id": row["message_id"],
        "type": row["type"],
        "file_name": row["file_name"],
        "telegram_file_id": row["telegram_file_id"],
        "storage_key": row["storage_key"],
        "mime_type": row["mime_type"],
        "file_size": row["file_size"],
        "duration": row["duration"],
        "width": row["width"],
        "height": row["height"],
        "transcription": row["transcription"],
        "transcription_status": row["transcription_status"],
        "description": row["description"],
        "description_status": row["description_status"],
        "downloaded_at": _iso(row["downloaded_at"]),
        "file_deleted_at": _iso(row["file_deleted_at"]),
        "file_available": row["storage_key"] is not None
                          and row["file_deleted_at"] is None,
    }


# ─── GET /media/{id} ─────────────────────────────────────────────────

@router.get("/media/{media_id}")
async def get_media(media_id: int):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM media WHERE id = $1", media_id,
        )
    if row is None:
        raise HTTPException(404, {"error": {"code": "MEDIA_NOT_FOUND"}})
    return _media_to_dict(row)


# ─── GET /media/{id}/file ────────────────────────────────────────────

def _safe_filename(mime: str | None, media_id: int, file_name: str | None) -> str:
    """Имя для Content-Disposition. file_name из БД может быть None или с
    опасными символами — для простоты подставим media_id + расширение из mime."""
    if file_name:
        # Базовая санитизация — убираем управляющие и слэши.
        safe = "".join(
            c for c in file_name if c.isalnum() or c in (" ", ".", "-", "_")
        ).strip() or None
        if safe:
            return safe
    if mime and "/" in mime:
        ext = mime.split("/", 1)[1].split(";")[0]
        return f"media_{media_id}.{ext}"
    return f"media_{media_id}.bin"


@router.get("/media/{media_id}/file")
async def get_media_file(media_id: int):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, storage_key, mime_type, file_name, file_deleted_at
            FROM media WHERE id = $1
            """,
            media_id,
        )
    if row is None:
        raise HTTPException(404, {"error": {"code": "MEDIA_NOT_FOUND"}})

    if not row["storage_key"] or row["file_deleted_at"] is not None:
        raise HTTPException(410, {
            "error": {
                "code": "FILE_CLEANED",
                "message": "Файл удалён, метаданные и транскрипт остались",
            }
        })

    # Скачиваем байтами — дёшево и без танцев со стримами MinIO.
    # Файлы у нас ограничены Telegram'ом (20 MB для обычных, 2 GB Premium).
    try:
        data = await minio_mod.get_object(row["storage_key"])
    except Exception as e:
        raise HTTPException(500, {
            "error": {"code": "STORAGE_ERROR", "message": str(e)},
        })

    mime = row["mime_type"] or "application/octet-stream"
    fname = _safe_filename(mime, media_id, row["file_name"])

    def _gen():
        # Один чанк — чистый read через get_object уже вернул всё в память.
        yield data

    return StreamingResponse(
        _gen(),
        media_type=mime,
        headers={
            "Content-Length": str(len(data)),
            "Content-Disposition": f'inline; filename="{fname}"',
        },
    )


# ─── POST /media/{id}/retranscribe + /redescribe ─────────────────────

async def _reprocess(media_id: int, *, field: str) -> dict[str, Any]:
    """
    Общая реализация для двух endpoints. field = "transcription" | "description".
    Проверяет что media существует, тип подходит, файл ещё не удалён,
    сбрасывает статус и публикует media.reprocess.requested.
    """
    allowed = _TRANSCRIBE_TYPES if field == "transcription" else _DESCRIBE_TYPES
    status_col = f"{field}_status"

    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT id, type, storage_key, file_deleted_at, {status_col}
            FROM media WHERE id = $1
            """,
            media_id,
        )
        if row is None:
            raise HTTPException(404, {"error": {"code": "MEDIA_NOT_FOUND"}})

        if row["type"] not in allowed:
            raise HTTPException(409, {
                "error": {
                    "code": "WRONG_MEDIA_TYPE",
                    "message": f"type={row['type']} не подходит для {field}",
                }
            })

        if not row["storage_key"] or row["file_deleted_at"] is not None:
            raise HTTPException(410, {"error": {"code": "FILE_CLEANED"}})

        # Сбрасываем статус в pending — history при следующем *.done его
        # перепишет в done/failed.
        await conn.execute(
            f"UPDATE media SET {status_col} = 'pending' WHERE id = $1",
            media_id,
        )

    event = await bus.publish(
        module=Module.API,
        type=EventType.MEDIA_REPROCESS_REQUESTED,
        status=Status.IN_PROGRESS,
        data={"media_id": media_id, "field": field},
    )
    return {"media_id": media_id, "status": "pending", "event_id": event["id"]}


@router.post("/media/{media_id}/retranscribe")
async def retranscribe_media(media_id: int):
    return await _reprocess(media_id, field="transcription")


@router.post("/media/{media_id}/redescribe")
async def redescribe_media(media_id: int):
    return await _reprocess(media_id, field="description")
