"""
Bulk-операция: снести все диалоги одного аккаунта.

Используется ручкой `POST /accounts/{id}/dialogs/wipe?confirm=CONFIRM`
(см. api/routes/accounts.py). Для каждого диалога:
  1. Если он есть в текущем Telegram dialog list — `wrapper.delete_dialog(revoke=True)`.
  2. Если нет (диалог локально остался, а в Telegram его уже нет) —
     tg-side пропускаем, делаем только локальный wipe.
  3. Локальный wipe — переиспользуем `purge_dialog_locally` из routes.py
     (тот же flow, что у штучного DELETE /dialogs/{id}).

Категории в ответе:
  - deleted_full        — TG + local OK
  - deleted_local_only  — TG не было (already_gone), local OK
  - failed              — TG-side реальная ошибка (не «нет entity») ИЛИ
                          local purge упал. UI красит это в красный.

FloodWait: <=30s — sleep+retry; >30s — обрываем цикл с partial result.
Между диалогами пауза 300мс (страховка от rate limit).

См. docs/dev_diary/9.13_wipe_all_dialogs.md (исходно) и 9.14 (фикс).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon.errors import FloodWaitError

from core import bus, db
from core.events import EventType, Module, Status
from modules.worker.wrapper import (
    SessionExpired as WrapperSessionExpired,
    WrapperError,
)

log = logging.getLogger(__name__)


_FLOOD_WAIT_SHORT_SEC = 30
_INTER_DELETE_SLEEP_SEC = 0.3


async def wipe_all_dialogs(
    account_id: int,
    *,
    request: Any,
    confirm: str,
    revoke: bool = True,
) -> dict[str, Any]:
    """
    Удалить все диалоги аккаунта на стороне Telegram + локально.

    Бросает (ValueError с кодом):
      - INVALID_CONFIRM      — confirm != "CONFIRM".
      - ACCOUNT_NOT_FOUND    — нет такого account_id.
      - WORKER_NOT_RUNNING   — у аккаунта нет живого wrapper'а.

    Возвращает агрегат (см. модульный docstring).
    """
    if confirm != "CONFIRM":
        raise ValueError("INVALID_CONFIRM")

    # Ленивый импорт — избегаем циклической зависимости routes <-> wipe.
    from .routes import purge_dialog_locally

    pool = db.get_pool()
    async with pool.acquire() as conn:
        acc = await conn.fetchrow("SELECT id FROM accounts WHERE id = $1", account_id)
        if acc is None:
            raise ValueError("ACCOUNT_NOT_FOUND")
        dialogs = await conn.fetch(
            """
            SELECT id, telegram_user_id, username
            FROM dialogs
            WHERE account_id = $1
            ORDER BY id ASC
            """,
            account_id,
        )

    worker_manager = request.app.state.worker_manager
    autochat_service = request.app.state.autochat_service
    wrapper = worker_manager.get_wrapper(account_id)
    if wrapper is None:
        raise ValueError("WORKER_NOT_RUNNING")

    # Прогрев кэша + срез текущих peer'ов в TG dialog list. Best-effort:
    # если прогрев упал — продолжаем без фильтрации (tg_present_ids=None),
    # и решение «делать tg-side» принимаем оптимистично, ловя ValueError
    # уже при самом delete_dialog'е.
    tg_present_ids: set[int] | None
    try:
        tg_present_ids = await wrapper.prime_entity_cache_and_list()
        log.info(
            "wipe: account=%s primed cache, %d tg dialogs visible",
            account_id, len(tg_present_ids),
        )
    except WrapperSessionExpired:
        raise ValueError("WORKER_NOT_RUNNING")
    except Exception:
        log.exception("wipe: account=%s prime cache failed (non-fatal)", account_id)
        tg_present_ids = None

    deleted_full = 0
    deleted_local_only = 0
    failed: list[dict[str, Any]] = []
    stopped = False
    reason: str | None = None
    retry_after: int | None = None

    log.info(
        "wipe: account=%s starting (local_total=%d, revoke=%s)",
        account_id, len(dialogs), revoke,
    )

    for d in dialogs:
        dialog_id = int(d["id"])
        tg_user_id = int(d["telegram_user_id"])

        # Решаем: пытаться ли tg-side delete.
        in_tg = (tg_present_ids is None) or (tg_user_id in tg_present_ids)

        tg_side_ok = False
        tg_side_skipped_not_found = False

        if in_tg:
            try:
                await _delete_on_telegram_with_retry(wrapper, tg_user_id, revoke=revoke)
                tg_side_ok = True
            except _FloodWaitTooLong as e:
                stopped = True
                reason = "flood_wait"
                retry_after = e.seconds
                log.warning(
                    "wipe: account=%s stopped on dialog=%s (FloodWait %ss)",
                    account_id, dialog_id, e.seconds,
                )
                break
            except WrapperSessionExpired as e:
                stopped = True
                reason = "session_expired"
                failed.append({
                    "dialog_id": dialog_id, "telegram_user_id": tg_user_id,
                    "stage": "tg_delete", "error": f"session_expired: {e}",
                })
                log.warning("wipe: account=%s session_expired, stopping", account_id)
                break
            except _EntityNotFound:
                # tg уже не знает этот peer — относимся как к "already gone".
                tg_side_skipped_not_found = True
                log.info(
                    "wipe: account=%s tg entity gone for dialog=%s, local-only",
                    account_id, dialog_id,
                )
            except (WrapperError, Exception) as e:
                log.exception(
                    "wipe: account=%s tg-side delete failed for dialog=%s",
                    account_id, dialog_id,
                )
                failed.append({
                    "dialog_id": dialog_id, "telegram_user_id": tg_user_id,
                    "stage": "tg_delete", "error": str(e)[:300],
                })
                # Идём дальше — пробуем хотя бы локальную чистку.
        else:
            tg_side_skipped_not_found = True
            log.info(
                "wipe: account=%s tg-side skipped for dialog=%s (not in TG list)",
                account_id, dialog_id,
            )

        # Локальная чистка делается всегда (если не сломали break-ом выше).
        try:
            await purge_dialog_locally(
                dialog_id, request=request, autochat_service=autochat_service,
            )
            if tg_side_ok:
                deleted_full += 1
            elif tg_side_skipped_not_found:
                deleted_local_only += 1
            # Иначе: tg-side упал с реальной ошибкой → она уже в failed.
            # Локальный purge всё-таки прошёл — это лучше чем оставлять
            # неконсистентное состояние, и счётчик deleted_local_only тут
            # не растёт (чтобы не мешать с реально «чистыми» случаями).
        except Exception as e:
            log.exception(
                "wipe: account=%s local purge failed for dialog=%s",
                account_id, dialog_id,
            )
            failed.append({
                "dialog_id": dialog_id, "telegram_user_id": tg_user_id,
                "stage": "local_purge", "error": str(e)[:300],
            })

        await asyncio.sleep(_INTER_DELETE_SLEEP_SEC)

    summary = {
        "account_id": account_id,
        "deleted_full": deleted_full,
        "deleted_local_only": deleted_local_only,
        "deleted": deleted_full + deleted_local_only,  # обратная совместимость
        "failed": failed,
        "stopped": stopped,
        "reason": reason,
        "retry_after_seconds": retry_after,
        "processed_total": len(dialogs),
    }

    await bus.publish(
        module=Module.API,
        type=EventType.ACCOUNT_DIALOGS_WIPED,
        status=Status.SUCCESS if (not failed and not stopped) else Status.ERROR,
        account_id=account_id,
        data={
            "deleted_full": deleted_full,
            "deleted_local_only": deleted_local_only,
            "failed_count": len(failed),
            "stopped": stopped,
            "reason": reason,
            "retry_after_seconds": retry_after,
            "processed_total": len(dialogs),
            "revoke": revoke,
        },
    )

    log.info(
        "wipe: account=%s done (full=%d, local_only=%d, failed=%d, stopped=%s)",
        account_id, deleted_full, deleted_local_only, len(failed), stopped,
    )
    return summary


# ─────────────────────────────────────────────────────────────────────
# Внутренние утилиты
# ─────────────────────────────────────────────────────────────────────

class _FloodWaitTooLong(Exception):
    """Сигнал «слишком долгая задержка — выходим с partial result»."""
    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait {seconds}s")
        self.seconds = seconds


class _EntityNotFound(Exception):
    """Telegram не знает access_hash для этого peer'a — tg-side пропускаем."""


# Telethon бросает обычный ValueError со специфическим текстом для случая,
# когда entity нельзя зарезолвить. Отдельного исключения у него нет — ловим
# по подстроке. (Альтернатива: catch-all и потом классификация — но текст
# стабильный и в Telethon специально оставлен таким.)
_ENTITY_NOT_FOUND_MARKER = "Could not find the input entity"


async def _delete_on_telegram_with_retry(
    wrapper: Any, tg_user_id: int, *, revoke: bool,
) -> None:
    """
    Одна попытка delete + (опционально) один retry после короткого
    FloodWait. Длинный FloodWait → _FloodWaitTooLong. Ошибка резолва
    entity → _EntityNotFound (внешний цикл считает такой случай
    `deleted_local_only`, а не failure).
    """
    try:
        await wrapper.delete_dialog(tg_user_id, revoke=revoke)
        return
    except FloodWaitError as e:
        wait = int(getattr(e, "seconds", 0) or 0)
        if wait > _FLOOD_WAIT_SHORT_SEC:
            raise _FloodWaitTooLong(wait) from e
        log.info("wipe: short FloodWait %ss on user=%s, retrying", wait, tg_user_id)
        await asyncio.sleep(wait + 1)
    except ValueError as e:
        if _ENTITY_NOT_FOUND_MARKER in str(e):
            raise _EntityNotFound() from e
        raise

    # Один retry после короткого FloodWait.
    try:
        await wrapper.delete_dialog(tg_user_id, revoke=revoke)
    except FloodWaitError as e:
        wait = int(getattr(e, "seconds", 0) or 0)
        raise _FloodWaitTooLong(wait) from e
    except ValueError as e:
        if _ENTITY_NOT_FOUND_MARKER in str(e):
            raise _EntityNotFound() from e
        raise
