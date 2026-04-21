"""
Endpoint'ы /workers/*. См. docs/api.md → раздел "Воркеры".
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from api.sse import sse_format, sse_heartbeat
from core import redis as redis_mod
from modules.worker_manager.service import (
    ConfirmationRequired,
    ManagerError,
    PUBSUB_CHANNEL,
    WorkerManager,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["workers"])


def get_manager(request: Request) -> WorkerManager:
    return request.app.state.worker_manager


def _err(e: ManagerError) -> JSONResponse:
    return JSONResponse(
        status_code=e.status_code,
        content={"error": {"code": e.code, "message": e.message}},
    )


# ─────────────────────────────────────────────────────────────────────

@router.get("/workers")
async def list_workers(manager: WorkerManager = Depends(get_manager)):
    return await manager.list_workers()


@router.post("/workers/{account_id}/start")
async def start_worker(
    account_id: int, manager: WorkerManager = Depends(get_manager),
):
    try:
        return await manager.start(account_id)
    except ManagerError as e:
        return _err(e)


@router.post("/workers/{account_id}/stop")
async def stop_worker(
    account_id: int, manager: WorkerManager = Depends(get_manager),
):
    try:
        return await manager.stop(account_id)
    except ManagerError as e:
        return _err(e)


@router.delete("/accounts/{account_id}")
async def delete_account(
    account_id: int,
    x_confirm_delete: str | None = Header(default=None, alias="X-Confirm-Delete"),
    manager: WorkerManager = Depends(get_manager),
):
    if (x_confirm_delete or "").lower() != "yes":
        return _err(ConfirmationRequired())
    try:
        return await manager.delete(account_id)
    except ManagerError as e:
        return _err(e)


# ─────────────────────────────────────────────────────────────────────
# SSE: стрим изменений статусов воркеров
#
# Объявлен ДО /workers/{...} — в этом роутере таких нет, но на всякий.
# ─────────────────────────────────────────────────────────────────────

@router.get("/workers/stream")
async def workers_stream(request: Request):
    """
    SSE-поток: event `worker.update` на каждое изменение статуса любого воркера.
    Реализован через Redis pubsub-канал "worker_updates".
    """
    redis = redis_mod.get_client()
    pubsub = redis.pubsub()
    await pubsub.subscribe(PUBSUB_CHANNEL)

    async def generator():
        try:
            yield sse_heartbeat()
            idle_counter = 0

            while True:
                if await request.is_disconnected():
                    return
                try:
                    msg = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0,
                    )
                except Exception:
                    yield sse_heartbeat()
                    await asyncio.sleep(1)
                    continue

                if msg is None:
                    idle_counter += 1
                    if idle_counter >= 30:
                        yield sse_heartbeat()
                        idle_counter = 0
                    continue

                idle_counter = 0
                raw = msg.get("data")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                yield sse_format(event="worker.update", data=payload)
        finally:
            try:
                await pubsub.unsubscribe(PUBSUB_CHANNEL)
                await pubsub.aclose()
            except Exception:
                pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )