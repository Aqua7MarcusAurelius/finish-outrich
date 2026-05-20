"""
Endpoint'ы системной группы. Health, stats, proxy-check, debug.

До Этапа 3 эти endpoint'ы жили прямо в api/main.py.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import bus, db
from core import minio as minio_mod
from core import redis as redis_mod
from core.config import settings
from core.proxy import check_socks5

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
async def system_health(request: Request):
    """Живы ли компоненты. 200 если всё ОК, 503 если что-то лежит."""
    pg_ok = await db.check_health()
    redis_ok = await redis_mod.check_health()
    minio_ok = await minio_mod.check_health()
    auth_ok = getattr(request.app.state, "auth_service", None) is not None

    components = {
        "postgres": "ok" if pg_ok else "down",
        "redis": "ok" if redis_ok else "down",
        "minio": "ok" if minio_ok else "down",
        "auth_module": "ok" if auth_ok else "down",
    }
    all_ok = all(v == "ok" for v in components.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "status": "ok" if all_ok else "degraded",
            "components": components,
        },
    )


@router.get("/stats")
async def system_stats():
    """Базовые цифры для дашборда."""
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
                "SELECT COUNT(*) FROM events_archive "
                "WHERE time >= NOW() - INTERVAL '1 hour'"
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


# ── Proxy check ───────────────────────────────────────────────────

class ProxyCheckIn(BaseModel):
    proxy: str | None = None
    proxies: list[str] | None = None


@router.post("/proxy-check")
async def proxy_check(payload: ProxyCheckIn):
    """
    Превалидация прокси для формы авторизации.

    Принимает либо одиночный `proxy`, либо массив `proxies`.
    Возвращает `{"results": [{proxy, ok, latency_ms|error}, ...]}`.
    """
    urls: list[str] = []
    if payload.proxy:
        urls.append(payload.proxy)
    if payload.proxies:
        urls.extend(payload.proxies)
    if not urls:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "PROXY_REQUIRED",
                              "message": "Укажите proxy или proxies"}},
        )
    results = await asyncio.gather(*[check_socks5(u) for u in urls])
    return {"results": results}


# ── Settings: чтение/запись таблицы `settings` ────────────────────
# Используется UI на странице /settings (10.11) — редактируются
# ключи partner_crm.* и lead_evaluator.*. Кладём в system, потому что
# нет отдельного "settings"-модуля: таблица читается из многих мест,
# а пишется только через эту ручку (UI).


@router.get("/settings")
async def list_settings(prefix: str | None = None):
    """
    Список настроек. Если задан `prefix` — фильтр (`prefix='partner_crm.'`).
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        if prefix:
            rows = await conn.fetch(
                "SELECT key, value, description, updated_at FROM settings "
                "WHERE key LIKE $1 ORDER BY key",
                f"{prefix}%",
            )
        else:
            rows = await conn.fetch(
                "SELECT key, value, description, updated_at FROM settings "
                "ORDER BY key",
            )
    return {
        "items": [
            {
                "key": r["key"],
                "value": r["value"],
                "description": r["description"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
    }


class SettingsPatch(BaseModel):
    # `updates` — мап key→value (всё текстом, как и в БД).
    updates: dict[str, str] = Field(default_factory=dict)


@router.patch("/settings")
async def patch_settings(payload: SettingsPatch):
    """
    Обновить несколько ключей одной транзакцией. Несуществующие ключи
    игнорируются (только UPDATE, никакого INSERT — гарантия что
    оператор не насоздаёт мусора).
    """
    if not payload.updates:
        return {"updated": 0}

    updated = 0
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for key, value in payload.updates.items():
                result = await conn.execute(
                    "UPDATE settings SET value = $2, updated_at = NOW() "
                    "WHERE key = $1",
                    key, str(value),
                )
                # asyncpg execute returns 'UPDATE N'
                if result.startswith("UPDATE ") and result != "UPDATE 0":
                    updated += 1
    return {"updated": updated}


# ── Debug: эмит события руками (только в development) ────────────

class EmitEventIn(BaseModel):
    module: str = "system"
    type: str = "system.test"
    status: str = "success"
    account_id: int | None = None
    parent_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


@router.post("/_debug/emit-event")
async def emit_test_event(payload: EmitEventIn):
    if settings.APP_ENV != "development":
        raise HTTPException(404, detail={"error": {"code": "NOT_FOUND"}})
    event = await bus.publish(
        module=payload.module,
        type=payload.type,
        status=payload.status,
        account_id=payload.account_id,
        parent_id=payload.parent_id,
        data=payload.data,
    )
    return event