"""
Точка входа FastAPI.

Этап 2: шина событий, архивный писатель, SSE /events/stream.
Этап 3: модуль авторизации, менеджер воркеров, /system/proxy-check.
Этап 4: модуль истории (consumer шины, запись dialogs/messages/media),
         чистильщик файлов MinIO, endpoints /accounts/*/dialogs, /dialogs/*, /messages/*.
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
from modules.history.cleaner import Cleaner
from modules.history.routes import router as history_router
from modules.history.service import HistoryService
from modules.worker_manager.routes import router as workers_router
from modules.worker_manager.service import WorkerManager

log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────
    await db.init_pool()
    await redis_mod.init_client()
    await minio_mod.init_client()

    archive_task = asyncio.create_task(bus.archive_writer_loop())
    app.state.archive_task = archive_task

    history_service = HistoryService()
    app.state.history_service = history_service
    history_task = asyncio.create_task(history_service.run())
    app.state.history_task = history_task

    cleaner = Cleaner()
    app.state.cleaner = cleaner
    cleaner_task = asyncio.create_task(cleaner.run())
    app.state.cleaner_task = cleaner_task

    app.state.auth_service = AuthService()
    app.state.worker_manager = WorkerManager()

    try:
        yield
    finally:
        # ── Shutdown ───────────────────────────────────────────────
        try:
            await app.state.worker_manager.shutdown()
        except Exception:
            log.exception("worker_manager shutdown error")

        try:
            await app.state.auth_service.shutdown()
        except Exception:
            log.exception("auth_service shutdown error")

        try:
            await cleaner.stop()
        except Exception:
            log.exception("cleaner stop error")

        cleaner_task.cancel()
        try:
            await cleaner_task
        except asyncio.CancelledError:
            pass

        try:
            await history_service.stop()
        except Exception:
            log.exception("history_service stop error")

        history_task.cancel()
        try:
            await history_task
        except asyncio.CancelledError:
            pass

        archive_task.cancel()
        try:
            await archive_task
        except asyncio.CancelledError:
            pass

        await db.close_pool()
        await redis_mod.close_client()


app = FastAPI(
    title="Telegram Automation Framework",
    version="0.4.0",
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

app.include_router(system_router)
app.include_router(events_router)
app.include_router(auth_router)
app.include_router(workers_router)
app.include_router(history_router)
