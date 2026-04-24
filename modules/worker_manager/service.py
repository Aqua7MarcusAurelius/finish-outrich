"""
Менеджер воркеров. Держит пул воркеров, управляет их жизненным циклом.

Статусы воркеров — в Redis, ключ worker:{account_id}:status.
Обновления статусов публикуются в Redis pubsub-канал "worker_updates"
для SSE /workers/stream.

См. docs/worker_manager.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from core import bus, db
from core import minio as minio_mod
from core import redis as redis_mod
from core.events import EventType, Module, Status
from modules.worker.wrapper import ProxyUnavailable, SessionExpired
from modules.worker.worker import Worker

log = logging.getLogger(__name__)

REDIS_STATUS_PREFIX = "worker:"
REDIS_STATUS_SUFFIX = ":status"
PUBSUB_CHANNEL = "worker_updates"

# Возможные статусы (из api.md → /workers)
STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_STOPPING = "stopping"
STATUS_STOPPED = "stopped"
STATUS_CRASHED = "crashed"
STATUS_SESSION_EXPIRED = "session_expired"


# ─────────────────────────────────────────────────────────────────────
# Исключения
# ─────────────────────────────────────────────────────────────────────

class ManagerError(Exception):
    code: str = "MANAGER_ERROR"
    message: str = ""
    status_code: int = 400

    def __init__(self, message: str | None = None):
        self.message = message or self.__class__.message or self.code
        super().__init__(self.message)


class AccountNotFound(ManagerError):
    code = "ACCOUNT_NOT_FOUND"
    message = "Аккаунт не найден"
    status_code = 404


class AccountInactive(ManagerError):
    code = "ACCOUNT_INACTIVE"
    message = "Аккаунт помечен как неактивный"
    status_code = 409


class AlreadyRunning(ManagerError):
    code = "ALREADY_RUNNING"
    message = "Воркер уже запущен"
    status_code = 409


class NotRunning(ManagerError):
    code = "NOT_RUNNING"
    message = "Воркер не запущен"
    status_code = 409


class ConfirmationRequired(ManagerError):
    code = "CONFIRMATION_REQUIRED"
    message = "Требуется заголовок X-Confirm-Delete: yes"
    status_code = 428


# ─────────────────────────────────────────────────────────────────────
# Запись менеджера об одном воркере
# ─────────────────────────────────────────────────────────────────────

@dataclass
class _Slot:
    worker: Worker
    task: asyncio.Task
    started_at: float
    restart_count: int = 0
    last_error: str | None = None


# ─────────────────────────────────────────────────────────────────────

class WorkerManager:
    def __init__(self) -> None:
        self._slots: dict[int, _Slot] = {}
        self._lock = asyncio.Lock()

    async def reconcile_on_boot(self) -> None:
        """Сбросить stale-статусы в Redis при старте приложения.

        После рестарта app-контейнера in-memory ``_slots`` пустой, но в
        Redis ключи ``worker:{id}:status`` остались с прошлого запуска.
        ``list_workers`` читает Redis и показывает воркеры как ``running``,
        хотя таска нет. ``stop`` в этом состоянии возвращает
        ``NOT_RUNNING`` (409), UI получает "kнопка не работает".

        Лечение — один проход при инициализации: любой не-терминальный
        статус при пустом slot превращается в ``stopped``. Воркеры в
        этом проекте стартуются вручную через ``POST /workers/{id}/start``,
        так что после reconcile пользователь видит честное ``stopped``.
        """
        pool = db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id FROM accounts")

        changed: list[int] = []
        for r in rows:
            acc_id = r["id"]
            status = await self._get_status(acc_id)
            if status in (STATUS_RUNNING, STATUS_STARTING, STATUS_STOPPING):
                await self._set_status(acc_id, STATUS_STOPPED)
                changed.append(acc_id)
        if changed:
            log.info("reconcile_on_boot: reset stale status for accounts=%s", changed)

    # ─── Публичные методы ──────────────────────────────────────────

    async def list_workers(self) -> list[dict[str, Any]]:
        """
        Аккаунты + статус из Redis.
        Показываем все аккаунты — независимо от того запущен ли воркер.
        """
        pool = db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, phone, is_active "
                "FROM accounts ORDER BY id"
            )

        out: list[dict[str, Any]] = []
        for r in rows:
            acc_id = r["id"]
            status = await self._get_status(acc_id) or STATUS_STOPPED
            slot = self._slots.get(acc_id)
            uptime = (
                int(time.monotonic() - slot.started_at)
                if slot and status == STATUS_RUNNING
                else 0
            )
            out.append({
                "account_id": acc_id,
                "name": r["name"],
                "phone": r["phone"],
                "is_active": r["is_active"],
                "status": status,
                "uptime_seconds": uptime,
                "last_error": slot.last_error if slot else None,
            })
        return out

    async def start(self, account_id: int) -> dict[str, Any]:
        async with self._lock:
            if account_id in self._slots:
                raise AlreadyRunning()

            pool = db.get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, name, session_data, proxy_primary, "
                    "proxy_fallback, is_active FROM accounts WHERE id = $1",
                    account_id,
                )
            if row is None:
                raise AccountNotFound()
            if not row["is_active"]:
                raise AccountInactive()

            await self._set_status(account_id, STATUS_STARTING)
            await self._spawn(
                account_id=account_id,
                account_name=row["name"],
                session_data=row["session_data"],
                proxy_primary=row["proxy_primary"],
                proxy_fallback=row["proxy_fallback"],
            )

        return {"account_id": account_id, "status": STATUS_STARTING}

    async def stop(self, account_id: int) -> dict[str, Any]:
        async with self._lock:
            slot = self._slots.get(account_id)
            if slot is None:
                raise NotRunning()
            await self._set_status(account_id, STATUS_STOPPING)
            await slot.worker.stop()

        # Ждём завершения таска — вне лока
        try:
            await asyncio.wait_for(slot.task, timeout=30)
        except asyncio.TimeoutError:
            log.warning("stop: account=%s timeout, cancelling task", account_id)
            slot.task.cancel()

        return {"account_id": account_id, "status": STATUS_STOPPING}

    async def delete(self, account_id: int) -> dict[str, Any]:
        """
        Полное необратимое удаление: воркер → MinIO → БД → Redis → шина.
        """
        # 1. Остановить если работает
        if account_id in self._slots:
            try:
                await self.stop(account_id)
            except NotRunning:
                pass

        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM accounts WHERE id = $1", account_id,
            )
            if row is None:
                raise AccountNotFound()

            # 2. Собрать storage_key всех файлов этого аккаунта
            storage_keys = await conn.fetch(
                """
                SELECT m.storage_key
                FROM media m
                JOIN messages ms ON ms.id = m.message_id
                JOIN dialogs d ON d.id = ms.dialog_id
                WHERE d.account_id = $1 AND m.storage_key IS NOT NULL
                """,
                account_id,
            )

        # 3. Удалить файлы из MinIO (best-effort)
        deleted_files = 0
        for r in storage_keys:
            try:
                await minio_mod.remove_object(r["storage_key"])
                deleted_files += 1
            except Exception:
                log.exception("delete: failed to remove %s", r["storage_key"])

        # 4. Каскад в БД
        async with pool.acquire() as conn:
            async with conn.transaction():
                stats = await conn.fetchrow(
                    """
                    WITH
                        d AS (SELECT id FROM dialogs WHERE account_id = $1),
                        msg_count AS (
                            SELECT COUNT(*) AS c FROM messages
                            WHERE dialog_id IN (SELECT id FROM d)
                        )
                    SELECT (SELECT c FROM msg_count) AS messages_count
                    """,
                    account_id,
                )
                await conn.execute(
                    "DELETE FROM accounts WHERE id = $1", account_id,
                )
                # FK ON DELETE CASCADE на dialogs/messages/media — снесёт
                # связанное. Если в миграции каскад не поставлен, см. замечание
                # в комментарии ниже.

        # 5. Удалить статус в Redis
        redis = redis_mod.get_client()
        await redis.delete(f"{REDIS_STATUS_PREFIX}{account_id}{REDIS_STATUS_SUFFIX}")

        # 6. Событие на шину
        await bus.publish(
            module=Module.WORKER_MANAGER,
            type=EventType.ACCOUNT_DELETED,
            status=Status.SUCCESS,
            account_id=account_id,
            data={
                "deleted_messages": stats["messages_count"] if stats else 0,
                "deleted_files": deleted_files,
            },
        )

        return {
            "deleted": True,
            "stats": {
                "deleted_messages": stats["messages_count"] if stats else 0,
                "deleted_files": deleted_files,
            },
        }

    def get_wrapper(self, account_id: int):
        """
        Вернуть живой TelegramWrapper для аккаунта. None если воркер
        не запущен или ещё/уже не подключён. Используется endpoints'ами
        модуля истории для send_message / read_message.
        """
        slot = self._slots.get(account_id)
        if slot is None:
            return None
        if not slot.worker.wrapper.is_connected():
            return None
        return slot.worker.wrapper

    async def shutdown(self) -> None:
        """Остановить все воркеры при shutdown приложения."""
        ids = list(self._slots.keys())
        for acc_id in ids:
            try:
                await self.stop(acc_id)
            except Exception:
                log.exception("shutdown: failed to stop %s", acc_id)

    # ─── Внутренности ──────────────────────────────────────────────

    async def _spawn(
        self,
        *,
        account_id: int,
        account_name: str,
        session_data: bytes | None,
        proxy_primary: str,
        proxy_fallback: str | None,
        is_restart: bool = False,
    ) -> None:
        worker = Worker(
            account_id=account_id,
            account_name=account_name,
            session_data=session_data,
            proxy_primary=proxy_primary,
            proxy_fallback=proxy_fallback,
        )

        async def _runner():
            try:
                await worker.run()
                # Штатное завершение — по stop()
                await self._set_status(account_id, STATUS_STOPPED)
                await bus.publish(
                    module=Module.WORKER,
                    type=EventType.WORKER_STOPPED,
                    status=Status.SUCCESS,
                    account_id=account_id,
                )
            except SessionExpired as e:
                await self._set_status(account_id, STATUS_SESSION_EXPIRED)
                log.warning(
                    "worker account=%s session expired: %s", account_id, e,
                )
                # account.session_expired уже опубликован враппером
            except ProxyUnavailable as e:
                await self._set_status(account_id, STATUS_CRASHED, error=str(e))
                # system.error уже опубликован враппером
                await self._publish_crashed(account_id, str(e))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("worker account=%s crashed", account_id)
                await self._set_status(account_id, STATUS_CRASHED, error=str(e))
                await self._publish_crashed(account_id, str(e))

                # Одна попытка авторестарта
                slot = self._slots.get(account_id)
                if slot and slot.restart_count == 0 and not is_restart:
                    slot.restart_count = 1
                    log.info("worker account=%s: attempting one restart", account_id)
                    # Запустим новый таск и заменим слот
                    try:
                        await self._spawn(
                            account_id=account_id,
                            account_name=account_name,
                            session_data=session_data,
                            proxy_primary=proxy_primary,
                            proxy_fallback=proxy_fallback,
                            is_restart=True,
                        )
                        return
                    except Exception:
                        log.exception(
                            "worker account=%s: restart failed", account_id,
                        )
            finally:
                # Удаляем слот только если это финальное завершение
                # (при авторестарте мы уже подставили новый слот выше)
                slot = self._slots.get(account_id)
                if slot and slot.worker is worker:
                    self._slots.pop(account_id, None)

        task = asyncio.create_task(_runner())

        self._slots[account_id] = _Slot(
            worker=worker,
            task=task,
            started_at=time.monotonic(),
            restart_count=1 if is_restart else 0,
        )

        # Сразу выставим running — событие worker.started публикует сам Worker
        # после успешного connect
        await self._set_status(account_id, STATUS_RUNNING)

    async def _publish_crashed(self, account_id: int, error: str) -> None:
        await bus.publish(
            module=Module.WORKER,
            type=EventType.WORKER_CRASHED,
            status=Status.ERROR,
            account_id=account_id,
            data={"error": error},
        )

    async def _set_status(
        self, account_id: int, status: str, *, error: str | None = None,
    ) -> None:
        redis = redis_mod.get_client()
        key = f"{REDIS_STATUS_PREFIX}{account_id}{REDIS_STATUS_SUFFIX}"

        slot = self._slots.get(account_id)
        if slot and error is not None:
            slot.last_error = error
        uptime = (
            int(time.monotonic() - slot.started_at) if slot and status == STATUS_RUNNING
            else 0
        )

        payload = {
            "account_id": account_id,
            "status": status,
            "uptime_seconds": uptime,
            "last_error": slot.last_error if slot else None,
            "updated_at": bus.now_utc().isoformat(),
        }
        await redis.set(
            key, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        # pubsub для SSE
        await redis.publish(
            PUBSUB_CHANNEL, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    async def _get_status(self, account_id: int) -> str | None:
        redis = redis_mod.get_client()
        raw = await redis.get(
            f"{REDIS_STATUS_PREFIX}{account_id}{REDIS_STATUS_SUFFIX}"
        )
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return json.loads(raw).get("status")
        except Exception:
            return None