"""HTTP-клиент партнёрской CRM.

Тонкая обёртка над httpx, общается с тремя группами эндпоинтов:
- GET /api/l1/preferences   — настройки L1 (max_concurrent_leads, gender_filter, ...)
- GET /api/l1/leads         — список наших уже-забранных лидов
- POST /api/l1/leads/request-new       — взять N лидов из общего пула
- POST /api/l1/leads/:username/start   — сообщить "беру в работу"
- POST /api/l1/leads/:username/status  — выставить финальный статус

Авторизация: `Authorization: Bearer <CRM_API_TOKEN>`.
Все вызовы async, таймаут 30 секунд.

Клиент **не** пишет в БД и не публикует на шину — только HTTP.
Эту работу выполняет service.py поверх клиента.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from core.config import settings
from .errors import CrmHttpError, NotConfigured, PARTNER_TERMINAL_STATUSES, InvalidStatus

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0


class PartnerCrmClient:
    """
    Asynchronous client for the partner CRM HTTP API.

    Создаётся один на всё приложение — у httpx есть keep-alive пулинг
    соединений. Закрывается в lifespan shutdown.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = (base_url or settings.CRM_BASE_URL).rstrip("/")
        self._token = token or settings.CRM_API_TOKEN
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def is_configured(self) -> bool:
        return bool(self._base_url) and bool(self._token)

    def _ensure_configured(self) -> None:
        if not self.is_configured():
            raise NotConfigured(
                "CRM_BASE_URL / CRM_API_TOKEN не заданы в .env — "
                "partner_crm работать не может"
            )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ─── REST вызовы ─────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self._ensure_configured()
        client = await self._get_client()
        try:
            resp = await client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise CrmHttpError(f"network error talking to CRM: {e}") from e

        if resp.status_code >= 400:
            body_preview: str = ""
            try:
                body_preview = resp.text[:500]
            except Exception:
                pass
            raise CrmHttpError(
                f"CRM responded {resp.status_code} on {method} {path}",
                status=resp.status_code,
                body=body_preview,
            )

        try:
            return resp.json()
        except Exception:
            # Эндпоинты L1 всегда возвращают JSON; если нет —
            # это аномалия, лог и пустой dict.
            log.warning(
                "partner_crm: non-JSON response from %s %s, body=%r",
                method, path, resp.text[:200],
            )
            return {}

    async def get_preferences(self) -> dict[str, Any]:
        return await self._request("GET", "/api/l1/preferences")

    async def list_leads(self, status: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if status:
            params["status"] = status
        result = await self._request("GET", "/api/l1/leads", params=params)
        return list(result.get("leads") or [])

    async def request_new_leads(self, count: int) -> dict[str, Any]:
        """
        Реально забирает лидов из пула CRM — они становятся "taken" нашими.

        Ответ от CRM содержит поле `assigned` (список объектов лида)
        и `count` (сколько реально удалось забрать).
        """
        if count <= 0:
            return {"assigned": [], "count": 0}
        return await self._request(
            "POST", "/api/l1/leads/request-new",
            json={"count": int(count)},
        )

    async def start_lead(self, username: str) -> dict[str, Any]:
        """Сообщить CRM "беру в работу" — статус становится in_progress."""
        return await self._request(
            "POST", f"/api/l1/leads/{username}/start",
        )

    async def set_lead_status(self, username: str, status: str) -> dict[str, Any]:
        """
        Финальный статус — `ready_for_transfer` / `invalid` / `no_response`.
        Также CRM принимает `in_progress` (промежуточный), но мы его не
        шлём — `start_lead` ставит его сам.
        """
        if status not in PARTNER_TERMINAL_STATUSES and status != "in_progress":
            raise InvalidStatus(
                f"Недопустимый статус для CRM: {status!r}. "
                f"Допустимы: {sorted(PARTNER_TERMINAL_STATUSES)} + in_progress"
            )
        return await self._request(
            "POST", f"/api/l1/leads/{username}/status",
            json={"status": status},
        )

    # ─── v2 API (docs/CRM_API_TOKEN_V2.md) ───────────────────────────

    async def list_l2_managers(self) -> list[dict[str, Any]]:
        """Активные L2-менеджеры партнёра: [{login, name}, ...].

        Нужен чтобы проверить, что логин из `report_families.l2_login`
        ещё жив — CRM отдаёт 400 `l2_not_found` на неактивного.
        """
        r = await self._request("GET", "/api/l1/l2-managers")
        managers = r.get("managers") if isinstance(r, dict) else r
        return list(managers or [])

    async def assign_to_l2(self, username: str, l2_login: str) -> dict[str, Any]:
        """Передать лида конкретному L2 (v2 §2.3).

        ВАЖНО: эндпоинт сам переводит лида в `transferred` — отдельный
        вызов `/status` не нужен и невозможен (менять статус лида,
        уже попавшего в финал, CRM запрещает).

        Ошибки CRM: 400 `l2_login_required` / `l2_not_found`,
        404 `lead_not_found` — приходят как CrmHttpError.
        """
        if not l2_login:
            raise InvalidStatus("assign_to_l2: пустой l2_login")
        return await self._request(
            "POST", f"/api/l1/leads/{username}/assign-l2",
            json={"l2_login": l2_login},
        )
