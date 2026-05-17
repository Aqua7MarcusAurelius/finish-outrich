"""Сервис модуля partner_crm.

Тут живёт **бизнес-логика** работы с партнёрской CRM:
- докачать N лидов из CRM в локальный пул `crm_lead_pool`;
- читать состояние пула (для UI и сторожа);
- послать "беру в работу" (POST /leads/:u/start) и "финал" (/status)
  с публикацией событий и записью аудита.

Сервис **не** знает про autochat / lead_evaluator — общается с ними
через шину. Не запускает фоновых задач — это делает poller.py (10.3)
и listener при терминальных статусах (10.8).

Owns: crm_lead_pool (INSERT/DELETE). Читает: settings, accounts.
"""
from __future__ import annotations

import logging
from typing import Any

from core import bus, db
from core.events import EventType, Module, Status
from .client import PartnerCrmClient
from .errors import (
    CrmHttpError,
    InvalidStatus,
    NotConfigured,
    PartnerCrmError,
    PARTNER_TERMINAL_STATUSES,
)

log = logging.getLogger(__name__)


class PartnerCrmService:
    """
    Кручусь как singleton в lifespan. Holds httpx client (keep-alive),
    предоставляет публичные методы остальным модулям.
    """

    def __init__(self, client: PartnerCrmClient | None = None) -> None:
        self._client = client or PartnerCrmClient()

    @property
    def client(self) -> PartnerCrmClient:
        return self._client

    def is_configured(self) -> bool:
        return self._client.is_configured()

    async def shutdown(self) -> None:
        await self._client.close()

    # ─── Пул лидов ───────────────────────────────────────────────────

    async def pool_size(self) -> int:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval("SELECT COUNT(*) FROM crm_lead_pool"))

    async def list_pool(self, *, limit: int = 200) -> list[dict[str, Any]]:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, username, gender, fetched_at "
                "FROM crm_lead_pool ORDER BY fetched_at ASC LIMIT $1",
                int(limit),
            )
        return [
            {
                "id": r["id"],
                "username": r["username"],
                "gender": r["gender"],
                "fetched_at": r["fetched_at"].isoformat(),
            }
            for r in rows
        ]

    async def get_preferences(self) -> dict[str, Any]:
        """Прозрачно прокидываем preferences CRM наверх (для UI)."""
        return await self._client.get_preferences()

    # ─── Дозалив пула из CRM ─────────────────────────────────────────

    async def refill_pool(self, count: int) -> dict[str, Any]:
        """
        POST /api/l1/leads/request-new + INSERT в crm_lead_pool.

        Возвращает: {requested, fetched, inserted, skipped_duplicates}.
        Не валится если часть лидов уже есть в пуле — просто пропускает.
        Публикует partner_crm.pool_refilled / partner_crm.refill_failed.
        """
        if count <= 0:
            return {"requested": 0, "fetched": 0, "inserted": 0,
                    "skipped_duplicates": 0}

        try:
            payload = await self._client.request_new_leads(count)
        except PartnerCrmError as e:
            await self._publish_failed(EventType.PARTNER_CRM_REFILL_FAILED, e)
            raise

        assigned = payload.get("assigned") or payload.get("leads") or []
        inserted = 0
        skipped = 0

        pool = db.get_pool()
        async with pool.acquire() as conn:
            for lead in assigned:
                username = lead.get("username")
                if not username:
                    skipped += 1
                    continue
                gender = lead.get("gender")
                if gender not in (None, "female", "male"):
                    gender = None
                # ON CONFLICT — если уже у нас в пуле, не дублируем.
                # CRM не должна выдавать одного и того же дважды,
                # но защищаемся на всякий.
                inserted_ok = await conn.fetchval(
                    """
                    INSERT INTO crm_lead_pool (username, gender)
                    VALUES ($1, $2)
                    ON CONFLICT (username) DO NOTHING
                    RETURNING id
                    """,
                    username, gender,
                )
                if inserted_ok is not None:
                    inserted += 1
                else:
                    skipped += 1

        result = {
            "requested": int(count),
            "fetched": len(assigned),
            "inserted": inserted,
            "skipped_duplicates": skipped,
        }

        await bus.publish(
            module=Module.PARTNER_CRM,
            type=EventType.PARTNER_CRM_POOL_REFILLED,
            status=Status.SUCCESS,
            data=result,
        )
        log.info("partner_crm: refill pool result=%s", result)
        return result

    async def claim_lead_for_account(
        self,
        *,
        account_id: int,
        target_gender: str,
    ) -> dict[str, Any] | None:
        """
        Забрать **одного** лида из пула под конкретного воркера с учётом
        фильтра по полу. Используется worker_auto_runner при тике.

        target_gender: 'any' | 'female' | 'male'.

        Возвращает {id, username, gender} или None если подходящего нет.
        """
        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if target_gender == "any":
                    where = "TRUE"
                    args: list[Any] = []
                elif target_gender in ("female", "male"):
                    # gender IS NULL — берём тоже (если CRM не сказала пол,
                    # считаем подходящим, лучше отправить чем держать в пуле).
                    where = "(gender IS NULL OR gender = $1)"
                    args = [target_gender]
                else:
                    log.warning(
                        "partner_crm: unknown target_gender=%r for account=%s, "
                        "treating as 'any'", target_gender, account_id,
                    )
                    where = "TRUE"
                    args = []

                row = await conn.fetchrow(
                    f"""
                    SELECT id, username, gender FROM crm_lead_pool
                    WHERE {where}
                    ORDER BY fetched_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """,
                    *args,
                )
                if row is None:
                    return None

                await conn.execute(
                    "DELETE FROM crm_lead_pool WHERE id = $1", row["id"],
                )

        claimed = {
            "id": int(row["id"]),
            "username": row["username"],
            "gender": row["gender"],
        }

        await bus.publish(
            module=Module.PARTNER_CRM,
            type=EventType.PARTNER_CRM_LEAD_TAKEN,
            status=Status.SUCCESS,
            account_id=account_id,
            data={
                "username": claimed["username"],
                "gender": claimed["gender"],
            },
        )
        return claimed

    # ─── Касания CRM ──────────────────────────────────────────────────

    async def push_lead_started(
        self, *, account_id: int | None, username: str,
    ) -> None:
        """
        Сказать CRM "взяли в работу". Зовём из autochat в момент
        фактического старта переписки.
        """
        try:
            await self._client.start_lead(username)
        except PartnerCrmError as e:
            await bus.publish(
                module=Module.PARTNER_CRM,
                type=EventType.PARTNER_CRM_LEAD_START_FAILED,
                status=Status.ERROR,
                account_id=account_id,
                data={"username": username, "error": str(e)},
            )
            raise

        await bus.publish(
            module=Module.PARTNER_CRM,
            type=EventType.PARTNER_CRM_LEAD_STARTED,
            status=Status.SUCCESS,
            account_id=account_id,
            data={"username": username},
        )

    async def push_lead_finalized(
        self,
        *,
        account_id: int | None,
        username: str,
        terminal_status: str,
    ) -> None:
        """
        Финальный пуш — `ready_for_transfer` / `invalid` / `no_response`.
        Зовётся listener'ом (10.8) при появлении dialog.status_changed
        с терминальным пузырём на CRM-лиде.
        """
        if terminal_status not in PARTNER_TERMINAL_STATUSES:
            raise InvalidStatus(
                f"push_lead_finalized: статус {terminal_status!r} не финальный"
            )

        try:
            await self._client.set_lead_status(username, terminal_status)
        except PartnerCrmError as e:
            await bus.publish(
                module=Module.PARTNER_CRM,
                type=EventType.PARTNER_CRM_STATUS_PUSH_FAILED,
                status=Status.ERROR,
                account_id=account_id,
                data={
                    "username": username,
                    "status": terminal_status,
                    "error": str(e),
                },
            )
            raise

        await bus.publish(
            module=Module.PARTNER_CRM,
            type=EventType.PARTNER_CRM_STATUS_PUSHED,
            status=Status.SUCCESS,
            account_id=account_id,
            data={"username": username, "status": terminal_status},
        )

    # ─── Внутреннее ──────────────────────────────────────────────────

    async def _publish_failed(
        self, event_type: str, exc: PartnerCrmError,
    ) -> None:
        data = {"error": str(exc), "code": exc.code}
        if isinstance(exc, CrmHttpError):
            data["http_status"] = exc.status
            data["body"] = exc.body
        await bus.publish(
            module=Module.PARTNER_CRM,
            type=event_type,
            status=Status.ERROR,
            data=data,
        )
