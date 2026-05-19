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
        Двухфазный рефилл пула.

        Фаза 1 — `GET /api/l1/leads?status=taken`: подтягиваем лидов,
                 которые CRM уже назначила нам (либо нашим прошлым
                 request-new'ом, либо без нашего запроса), но ещё не
                 переведены в in_progress.
        Фаза 2 — `POST /api/l1/leads/request-new {count: N}` на остаток
                 до целевого размера `count` (берётся pool_size после
                 фазы 1, разница идёт в request).

        На каждого УСПЕШНО взятого лида сразу `POST /leads/:u/start` →
        CRM-статус становится in_progress, событие
        `partner_crm.lead_started`. Если /start упал — лид НЕ кладётся
        в локальный пул (на следующем тике увидим его снова в taken
        и попробуем ещё).

        Возвращает агрегат по двум фазам.
        Публикует `partner_crm.pool_refilled` / `partner_crm.refill_failed`.
        """
        if count <= 0:
            return {
                "requested": 0,
                "from_assigned": 0,
                "from_request_new": 0,
                "inserted": 0,
                "skipped_duplicates": 0,
                "start_failed": 0,
            }

        # ── Фаза 1: уже назначенные нам, ещё не в работе ──────────────
        # Фильтр `?status=new` совпадает с `lead_status=new` — те лиды
        # что висят у нас на профиле (либо нам их назначили, либо мы
        # сами через request-new забрали), и которых мы ещё не двинули
        # в in_progress. (Workflow поле `status` нам неважно — оно
        # внутреннее у CRM.)
        assigned_taken: list[dict[str, Any]] = []
        try:
            assigned_taken = await self._client.list_leads(status="new")
        except PartnerCrmError as e:
            # Не валим всю операцию — пробуем хотя бы request-new
            log.warning("partner_crm refill: phase-1 list_leads failed: %s", e)

        phase1_inserted = 0
        phase1_skipped = 0
        phase1_start_failed = 0
        for lead in assigned_taken:
            r = await self._take_into_pool(lead, origin="assigned")
            if r == "inserted":
                phase1_inserted += 1
            elif r == "skipped":
                phase1_skipped += 1
            elif r == "start_failed":
                phase1_start_failed += 1

        # ── Фаза 2: дозалив через request-new если ещё мало ───────────
        current_size = await self.pool_size()
        needed = max(0, int(count) - current_size)

        phase2_fetched = 0
        phase2_inserted = 0
        phase2_skipped = 0
        phase2_start_failed = 0
        if needed > 0:
            try:
                payload = await self._client.request_new_leads(needed)
            except PartnerCrmError as e:
                await self._publish_failed(EventType.PARTNER_CRM_REFILL_FAILED, e)
                # Если первая фаза что-то сделала — публикуем результат с
                # пометкой что вторая упала, иначе пробрасываем ошибку.
                if phase1_inserted == 0:
                    raise
                payload = {"assigned": [], "leads": [], "count": 0}

            new_leads = payload.get("assigned") or payload.get("leads") or []
            phase2_fetched = len(new_leads)
            for lead in new_leads:
                r = await self._take_into_pool(lead, origin="request_new")
                if r == "inserted":
                    phase2_inserted += 1
                elif r == "skipped":
                    phase2_skipped += 1
                elif r == "start_failed":
                    phase2_start_failed += 1

        result = {
            "requested": int(count),
            "from_assigned": len(assigned_taken),
            "from_request_new": phase2_fetched,
            "inserted": phase1_inserted + phase2_inserted,
            "skipped_duplicates": phase1_skipped + phase2_skipped,
            "start_failed": phase1_start_failed + phase2_start_failed,
        }

        await bus.publish(
            module=Module.PARTNER_CRM,
            type=EventType.PARTNER_CRM_POOL_REFILLED,
            status=Status.SUCCESS,
            data=result,
        )
        log.info("partner_crm: refill pool result=%s", result)
        return result

    async def _take_into_pool(
        self, lead: dict[str, Any], *, origin: str,
    ) -> str:
        """
        Взять одного лида в локальный пул: сначала `POST /leads/:u/start`,
        потом INSERT в `crm_lead_pool`.

        Порядок важен: если /start упадёт, в локальный пул не кладём —
        иначе будем держать у себя лида, который для CRM «не в работе»,
        и это рассинхрон.

        origin — для аудита в `partner_crm.lead_started` ('assigned' /
        'request_new' / 'manual_refill'). На event payload не влияет
        фатально, удобно для фильтрации в /events.

        Возвращает 'inserted' | 'skipped' | 'start_failed' | 'invalid'.
        """
        username = lead.get("username")
        if not username:
            return "invalid"

        gender = lead.get("gender")
        if gender not in (None, "female", "male"):
            gender = None

        # 1) Переводим в in_progress первым — чтобы локально не держать
        # лида, которого CRM не подтвердила «в работе».
        # Используем /status {in_progress}, а не /start: /start у CRM
        # требует привязанного L1-TG аккаунта на их стороне (которого у
        # нас нет — мы общаемся через свой Telethon). /status работает
        # без привязки и просто меняет lead_status.
        try:
            await self._client.set_lead_status(username, "in_progress")
        except PartnerCrmError as e:
            log.warning(
                "partner_crm refill: set in_progress failed for %s: %s",
                username, e,
            )
            await bus.publish(
                module=Module.PARTNER_CRM,
                type=EventType.PARTNER_CRM_LEAD_START_FAILED,
                status=Status.ERROR,
                data={"username": username, "error": str(e), "origin": origin},
            )
            return "start_failed"

        await bus.publish(
            module=Module.PARTNER_CRM,
            type=EventType.PARTNER_CRM_LEAD_STARTED,
            status=Status.SUCCESS,
            data={"username": username, "origin": origin},
        )

        # 2) INSERT в пул. ON CONFLICT — если уже там, пропускаем (это
        # нормально: /start был успешным, статус в CRM теперь in_progress,
        # лид и так у нас в очереди).
        pool = db.get_pool()
        async with pool.acquire() as conn:
            inserted_id = await conn.fetchval(
                """
                INSERT INTO crm_lead_pool (username, gender)
                VALUES ($1, $2)
                ON CONFLICT (username) DO NOTHING
                RETURNING id
                """,
                username, gender,
            )

        return "inserted" if inserted_id is not None else "skipped"

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
