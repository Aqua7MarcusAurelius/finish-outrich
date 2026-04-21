"""
Точка входа FastAPI.

Этап 2: шина событий, архивный писатель, SSE /events/stream.
Этап 3: модуль авторизации (/auth/*), /system/proxy-check.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.events import router as events_router
from api.routes.system import router as system_router
from core import bus, db
from core import minio as minio_mod
from core import redis as redis_mod
from core.config import settings
from modules.auth.routes import router as auth_router
from modules.auth.service import AuthService

log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────
    await db.init_pool()
    await redis_mod.init_client()
    await minio_mod.init_client()

    archive_task = asyncio.create_task(bus.archive_writer_loop())
    app.state.archive_task = archive_task

    app.state.auth_service = AuthService()

    try:
        yield
    finally:
        # ── Shutdown ───────────────────────────────────────────────
        try:
            await app.state.auth_service.shutdown()
        except Exception:
            log.exception("auth_service shutdown error")

        archive_task.cancel()
        try:
            await archive_task
        except asyncio.CancelledError:
            pass

        await db.close_pool()
        await redis_mod.close_client()


app = FastAPI(
    title="Telegram Automation Framework",
    version="0.3.0",
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
app.include_router(system_router)
app.include_router(events_router)
app.include_router(auth_router)