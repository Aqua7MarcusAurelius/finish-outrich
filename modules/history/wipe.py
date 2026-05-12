"""
Bulk-операция: снести все диалоги одного аккаунта.

Используется ручкой `POST /accounts/{id}/dialogs/wipe?confirm=CONFIRM`
(см. routes.py). Делает две вещи на каждый диалог:
  1. Удаление на стороне Telegram через wrapper.delete_dialog(revoke=True)
     — стирает историю и у собеседника тоже.
  2. Локальную зачистку — переиспользует purge_dialog_locally из routes.py
     (тот же flow, что у штучного DELETE /dialogs/{id}).

FloodWait: при коротких задержках (<= 30s) делаем sleep+retry того же
диалога; при длинных — останавливаемся и возвращаем партиал, чтобы
оператор повторил после паузы. Любая другая ошибка на конкретном
диалоге — складываем в `failed`, но цикл продолжаем.

См. docs/dev_diary/9.13_wipe_all_dialogs.md.
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


# Короткий FloodWait — спим и ретраим. Длинный — выходим с partial result.
_FLOOD_WAIT_SHORT_SEC = 30
# Пауза между удалениями — дешёвая защита от попадания в rate limit.
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

    Бросает:
      - ValueError("INVALID_CONFIRM") — если confirm != "CONFIRM".
      - ValueError("WORKER_NOT_RUNNING") — если у аккаунта нет живого wrapper'а.
      - ValueError("ACCOUNT_NOT_FOUND") — если такого account_id нет в БД.

    Возвращает dict:
        {
            "account_id": int,
            "deleted": int,                       # сколько успешно
            "failed": [{dialog_id, telegram_user_id, error}],
            "stopped": bool,                      # True если прерывание по long FloodWait
            "reason": str | None,                 # "flood_wait" если stopped
            "retry_after_seconds": int | None,    # сколько ждать перед повтором
            "processed_total": int,               # сколько диалогов взяли в работу
        }
    """
    if confirm != "CONFIRM":
        raise ValueError("INVALID_CONFIRM")

    # Импорт внутри — избегаем циклической зависимости routes <-> wipe.
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

    deleted = 0
    failed: list[dict[str, Any]] = []
    stopped = False
    reason: str | None = None
    retry_after: int | None = None

    log.info(
        "wipe: account=%s starting (total=%d, revoke=%s)",
        account_id, len(dialogs), revoke,
    )

    for d in dialogs:
        dialog_id = int(d["id"])
        tg_user_id = int(d["telegram_user_id"])

        # Telegram-side delete с одной попыткой retry на короткий FloodWait.
        try:
            await _delete_on_telegram_with_retry(wrapper, tg_user_id, revoke=revoke)
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
            # Сессия протухла — продолжать смысла нет, всё дальше упадёт.
            stopped = True
            reason = "session_expired"
            failed.append({
                "dialog_id": dialog_id, "telegram_user_id": tg_user_id,
                "error": f"session_expired: {e}",
            })
            log.warning("wipe: account=%s session_expired, stopping", account_id)
            break
        except (WrapperError, Exception) as e:
            # Прочие ошибки на конкретном диалоге — фиксируем и идём дальше.
            # Локальный wipe всё равно сделаем — там нет внешних зависимостей.
            log.exception(
                "wipe: account=%s tg-side delete failed for dialog=%s",
                account_id, dialog_id,
            )
            failed.append({
                "dialog_id": dialog_id, "telegram_user_id": tg_user_id,
                "error": f"tg_delete_failed: {e}",
            })
            # Намеренно идём дальше — пробуем локальную чистку.

        # Локальная чистка.
        try:
            await purge_dialog_locally(
                dialog_id, request=request, autochat_service=autochat_service,
            )
            deleted += 1
        except Exception as e:
            log.exception(
                "wipe: account=%s local purge failed for dialog=%s",
                account_id, dialog_id,
            )
            failed.append({
                "dialog_id": dialog_id, "telegram_user_id": tg_user_id,
                "error": f"local_purge_failed: {e}",
            })

        # Маленькая пауза между диалогами — снижает шанс FloodWait.
        await asyncio.sleep(_INTER_DELETE_SLEEP_SEC)

    summary = {
        "account_id": account_id,
        "deleted": deleted,
        "failed": failed,
        "stopped": stopped,
        "reason": reason,
        "retry_after_seconds": retry_after,
        "processed_total": len(dialogs),
    }

    # Агрегирующее событие в шину.
    await bus.publish(
        module=Module.API,
        type=EventType.ACCOUNT_DIALOGS_WIPED,
        status=Status.SUCCESS if not failed and not stopped else Status.ERROR,
        account_id=account_id,
        data={
            "deleted": deleted,
            "failed_count": len(failed),
            "stopped": stopped,
            "reason": reason,
            "retry_after_seconds": retry_after,
            "processed_total": len(dialogs),
            "revoke": revoke,
        },
    )

    log.info(
        "wipe: account=%s done (deleted=%d, failed=%d, stopped=%s)",
        account_id, deleted, len(failed), stopped,
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


async def _delete_on_telegram_with_retry(
    wrapper: Any, tg_user_id: int, *, revoke: bool,
) -> None:
    """
    Одна попытка delete + (опционально) один retry после короткого
    FloodWait. Длинный FloodWait → _FloodWaitTooLong (внешний цикл
    останавливается и возвращает partial result).
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

    # Ровно один retry — если снова FloodWait, считаем «длинной» защитой
    # от бесконечного цикла.
    try:
        await wrapper.delete_dialog(tg_user_id, revoke=revoke)
    except FloodWaitError as e:
        wait = int(getattr(e, "seconds", 0) or 0)
        raise _FloodWaitTooLong(wait) from e
