"""
Точка входа FastAPI.

Этап 2: подключена шина событий, фоновый архивный писатель,
SSE-поток /events/stream, endpoints /events и /events/{id},
debug-endpoint /system/_debug/emit-event для ручной проверки.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.routes.events import router as events_router
from core import bus, db
from core import minio as minio_mod
from core import redis as redis_mod
from core.config import settings

log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: инфраструктура
    await db.init_pool()
    await redis_mod.init_client()
    await minio_mod.init_client()

    # Фоновая задача: читает Redis Stream и пишет в events_archive
    archive_task = asyncio.create_task(bus.archive_writer_loop())
    app.state.archive_task = archive_task

    try:
        yield
    finally:
        # Shutdown: аккуратно гасим фоновую задачу
        archive_task.cancel()
        try:
            await archive_task
        except asyncio.CancelledError:
            pass

        await db.close_pool()
        await redis_mod.close_client()


app = FastAPI(
    title="Telegram Automation Framework",
    version="0.2.0",
    docs_url="/docs" if settings.DOCS_PUBLIC else None,
    redoc_url="/redoc" if settings.DOCS_PUBLIC else None,
    openapi_url="/openapi.json" if settings.DOCS_PUBLIC else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Confirm-Delete", "Last-Event-ID"],
)

# Роутеры
app.include_router(events_router)


# ─────────────────────────────────────────────────────────────────────
# System endpoints
# ─────────────────────────────────────────────────────────────────────

@app.get("/system/health", tags=["system"])
async def system_health():
    """Живы ли компоненты. 200 если всё ОК, 503 если что-то лежит."""
    pg_ok = await db.check_health()
    redis_ok = await redis_mod.check_health()
    minio_ok = await minio_mod.check_health()

    components = {
        "postgres": "ok" if pg_ok else "down",
        "redis": "ok" if redis_ok else "down",
        "minio": "ok" if minio_ok else "down",
        # Появится на этапе 3
        "auth_module": "ok",
    }
    all_ok = all(status == "ok" for status in components.values())

    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "status": "ok" if all_ok else "degraded",
            "components": components,
        },
    )


@app.get("/system/stats", tags=["system"])
async def system_stats():
    """Базовые цифры."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        accounts_total = await conn.fetchval("SELECT COUNT(*) FROM accounts") or 0
        accounts_active = (
            await conn.fetchval("SELECT COUNT(*) FROM accounts WHERE is_active = TRUE")
            or 0
        )
        dialogs_total = await conn.fetchval("SELECT COUNT(*) FROM dialogs") or 0
        messages_total = await conn.fetchval("SELECT COUNT(*) FROM messages") or 0
        media_total = await conn.fetchval("SELECT COUNT(*) FROM media") or 0
        media_pending_trans = (
            await conn.fetchval(
                "SELECT COUNT(*) FROM media WHERE transcription_status = 'pending'"
            )
            or 0
        )
        media_pending_desc = (
            await conn.fetchval(
                "SELECT COUNT(*) FROM media WHERE description_status = 'pending'"
            )
            or 0
        )
        events_last_hour = (
            await conn.fetchval(
                "SELECT COUNT(*) FROM events_archive WHERE time >= NOW() - INTERVAL '1 hour'"
            )
            or 0
        )

    return {
        "accounts": {
            "total": accounts_total,
            "active": accounts_active,
            "inactive": accounts_total - accounts_active,
        },
        "workers": {"running": 0, "stopped": 0, "crashed": 0},
        "data": {
            "dialogs_total": dialogs_total,
            "messages_total": messages_total,
            "media_total": media_total,
            "media_pending": {
                "transcription": media_pending_trans,
                "description": media_pending_desc,
            },
        },
        "events_last_hour": events_last_hour,
    }


# ─────────────────────────────────────────────────────────────────────
# Debug: эмит события руками (только в development)
# ─────────────────────────────────────────────────────────────────────

class EmitEventIn(BaseModel):
    module: str = "system"
    type: str = "system.test"
    status: str = "success"
    account_id: int | None = None
    parent_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


@app.post("/system/_debug/emit-event", tags=["system"])
async def emit_test_event(payload: EmitEventIn):
    """
    Опубликовать тестовое событие на шину.

    Доступен только при APP_ENV=development.
    Удобно для ручной проверки `/events`, `/events/stream` и archive writer.
    """
    if settings.APP_ENV != "development":
        raise HTTPException(404, {"error": {"code": "NOT_FOUND"}})

    event = await bus.publish(
        module=payload.module,
        type=payload.type,
        status=payload.status,
        account_id=payload.account_id,
        parent_id=payload.parent_id,
        data=payload.data,
    )
    return event
