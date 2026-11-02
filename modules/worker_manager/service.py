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
from core.config import settings
from core.events import EventType, Module, Status
from modules.worker.wrapper import (
    ProxyUnavailable,      # noqa: F401 (legacy auth-flow всё ещё использует)
    SessionExpired,
    TelethonDisconnected,  # noqa: F401 (тип оставлен для обратной совместимости)
)
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


class RecoveryDisabled(ManagerError):
    code = "RECOVERY_DISABLED"
    message = "Автовосстановление воркера не разрешено"
    status_code = 409


class ConfirmationRequired(ManagerError):
    code = "CONFIRMATION_REQUIRED"
    message = "Требуется заголовок X-Confirm-Delete: yes"
    status_code = 428


class ServiceAccountNotStartable(ManagerError):
    code = "SERVICE_ACCOUNT_NOT_STARTABLE"
    message = "Сервисный аккаунт доставки не запускается как воркер"
    status_code = 409


class AccountFingerprintMissing(ManagerError):
    code = "ACCOUNT_FINGERPRINT_MISSING"
    message = (
        "У аккаунта нет api_id / api_hash / device-info — "
        "переавторизуй через мастер добавления аккаунта"
    )
    status_code = 409


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
    stop_requested: bool = False
    suppress_stopped_event: bool = False


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
        статус при пустом slot превращается в ``stopped``. Сохранённое
        намерение worker_desired_running не меняется: последующий явный
        старт или boot-recover использует реальное состояние менеджера.
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
            # Сервис-аккаунт доставки (report_delivery) скрыт из UI — он не
            # лид-воркер, своим TG-подключением управляет модуль доставки.
            rows = await conn.fetch(
                "SELECT id, name, phone, is_active "
                "FROM accounts WHERE name <> $1 ORDER BY id",
                settings.SERVICE_ACCOUNT_NAME,
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

    async def readiness(self) -> dict[str, Any]:
        """Actual local tasks/connections, separate from cached UI status."""
        pool = db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, is_active, worker_desired_running FROM accounts "
                "WHERE name <> $1 ORDER BY id", settings.SERVICE_ACCOUNT_NAME,
            )
        workers = []
        for row in rows:
            account_id = row["id"]
            slot = self._slots.get(account_id)
            task_alive = bool(slot and not slot.task.done())
            connected = bool(task_alive and slot.worker.wrapper.is_connected())
            status = await self._get_status(account_id) or STATUS_STOPPED
            desired = bool(row["worker_desired_running"])
            workers.append({
                "account_id": account_id,
                "is_active": row["is_active"],
                "desired_running": desired,
                "status": status,
                "task_alive": task_alive,
                "connected": connected,
                "recoverable": bool(desired and row["is_active"]
                                    and status != STATUS_SESSION_EXPIRED),
            })
        desired_workers = [w for w in workers if w["desired_running"]]
        ready = all(w["connected"] for w in desired_workers)
        return {
            "ready": ready,
            "desired": len(desired_workers),
            "connected": sum(w["connected"] for w in workers),
            "desired_connected": sum(w["connected"] for w in desired_workers),
            "task_alive": sum(w["task_alive"] for w in workers),
            "workers": workers,
        }

    async def _set_desired_running(self, account_id: int, enabled: bool) -> None:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE accounts SET worker_desired_running = $2 WHERE id = $1",
                account_id, enabled,
            )
        if result == "UPDATE 0":
            raise AccountNotFound()

    async def start(
        self, account_id: int, *, recover: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            if account_id in self._slots:
                raise AlreadyRunning()

            pool = db.get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, name, session_data, proxy_primary, "
                    "proxy_fallback, is_active, api_id, api_hash, "
                    "device_model, system_version, app_version, "
                    "lang_code, system_lang_code, lang_pack, worker_desired_running "
                    "FROM accounts WHERE id = $1",
                    account_id,
                )
            if row is None:
                raise AccountNotFound()
            if row["name"] == settings.SERVICE_ACCOUNT_NAME:
                # Сервис-аккаунт доставки держит своё подключение в
                # report_delivery — второй коннект на ту же сессию запрещён.
                raise ServiceAccountNotStartable()
            if not row["is_active"]:
                raise AccountInactive()
            if recover:
                # Recheck under the same lock as manual stop. A previously
                # fetched readiness response is never authority to restart.
                if (not row["worker_desired_running"]
                        or await self._get_status(account_id) == STATUS_SESSION_EXPIRED):
                    raise RecoveryDisabled()
            if (not row["api_id"] or not row["api_hash"]
                    or not row["device_model"]
                    or not row["system_version"]
                    or not row["app_version"]):
                # У worker-аккаунта обязан быть индивидуальный api_id/hash
                # и device-info (фиксируем уникальный fingerprint, см. §9 SPEC).
                # Если NULL — это либо старая запись до миграции 0013, либо
                # ошибка при регистрации; запускать опасно — свалится при connect.
                raise AccountFingerprintMissing()

            if not recover:
                await self._set_desired_running(account_id, True)
            try:
                await self._set_status(account_id, STATUS_STARTING)
                await self._spawn(
                    account_id=account_id,
                    account_name=row["name"],
                    session_data=row["session_data"],
                    proxy_primary=row["proxy_primary"],
                    proxy_fallback=row["proxy_fallback"],
                    api_id=int(row["api_id"]),
                    api_hash=row["api_hash"],
                    device_model=row["device_model"],
                    system_version=row["system_version"],
                    app_version=row["app_version"],
                    lang_code=row["lang_code"] or "en",
                    system_lang_code=row["system_lang_code"] or "en",
                    lang_pack=row["lang_pack"] or "",
                )
            except BaseException:
                if not recover:
                    await self._set_desired_running(
                        account_id, bool(row["worker_desired_running"]),
                    )
                raise

        return {"account_id": account_id, "status": STATUS_STARTING}

    async def stop(
        self, account_id: int, *, preserve_intent: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            if not preserve_intent:
                # Even an offline account can be explicitly disabled. This
                # durable write precedes shutdown and prevents boot recovery.
                await self._set_desired_running(account_id, False)
            slot = self._slots.get(account_id)
            if slot is None:
                return {"account_id": account_id, "status": STATUS_STOPPED}
            # Process shutdown must not enqueue a destructive worker.stopped:
            # AutoRunner may consume it only after the next boot. A manual
            # stop wins over technical shutdown, regardless of their order.
            if not slot.stop_requested:
                slot.suppress_stopped_event = preserve_intent
            elif not preserve_intent:
                slot.suppress_stopped_event = False
            slot.stop_requested = True
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
                await self.stop(acc_id, preserve_intent=True)
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
        api_id: int,
        api_hash: str,
        device_model: str,
        system_version: str,
        app_version: str,
        lang_code: str = "en",
        system_lang_code: str = "en",
        lang_pack: str = "",
        is_restart: bool = False,
    ) -> None:
        # Колбэк для Worker.run: вызывается один раз после успешного connect'а.
        # Флипаем статус в Redis STARTING → RUNNING только тут, чтобы UI/
        # get_wrapper() не показывали "живой" воркер до реального подключения.
        async def _on_ready() -> None:
            await self._set_status(account_id, STATUS_RUNNING)

        worker = Worker(
            account_id=account_id,
            account_name=account_name,
            session_data=session_data,
            proxy_primary=proxy_primary,
            proxy_fallback=proxy_fallback,
            api_id=api_id,
            api_hash=api_hash,
            device_model=device_model,
            system_version=system_version,
            app_version=app_version,
            lang_code=lang_code,
            system_lang_code=system_lang_code,
            lang_pack=lang_pack,
            on_ready=_on_ready,
        )

        async def _runner():
            try:
                await worker.run()
                # Штатное завершение — по stop()
                async with self._lock:
                    # The old task must neither read a replacement slot's
                    # stop intent nor publish an event that stops its runner.
                    if self._slots.get(account_id) is owned_slot:
                        await self._set_status(account_id, STATUS_STOPPED)
                        if not owned_slot.suppress_stopped_event:
                            await bus.publish(
                                module=Module.WORKER,
                                type=EventType.WORKER_STOPPED,
                                status=Status.SUCCESS,
                                account_id=account_id,
                            )
            except SessionExpired as e:
                # Persist the block across Redis loss / host reboot. Only a
                # subsequent explicit start can opt this account in again.
                await self._set_desired_running(account_id, False)
                await self._set_status(account_id, STATUS_SESSION_EXPIRED)
                log.warning(
                    "worker account=%s session expired: %s", account_id, e,
                )
                # account.session_expired уже опубликован враппером
            # Fix #12: TelethonDisconnected больше НЕ пробрасывается из
            # Worker.run — новая resilient схема сама ретраит без exception'а
            # (как Telegram Desktop с "Connecting..."). ProxyUnavailable —
            # тоже: connect_via уже per-proxy, а старый connect() который
            # его бросал используется только в auth-flow. Runtime сетевые
            # ошибки → Worker.run ретраит и не выходит из run().
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("worker account=%s crashed", account_id)

                # Одна попытка прозрачного авторестарта.
                # L1 (2026-07-13): мутация _slots + спавн task под self._lock,
                # с перепроверкой что слот всё ещё принадлежит ЭТОМУ воркеру
                # (не перебит параллельным stop()/start()). L3: на время
                # восстановления статус STARTING, а не CRASHED — иначе UI
                # мигает «crashed» при живом авторестарте.
                restart_attempted = False
                spawned = False
                async with self._lock:
                    slot = self._slots.get(account_id)
                    if (
                        slot and slot.worker is worker
                        and slot.restart_count == 0 and not is_restart
                        and not slot.stop_requested
                    ):
                        slot.restart_count = 1
                        restart_attempted = True
                        await self._set_status(account_id, STATUS_STARTING)
                        log.info(
                            "worker account=%s: attempting one restart", account_id,
                        )
                        try:
                            await self._spawn(
                                account_id=account_id,
                                account_name=account_name,
                                session_data=session_data,
                                proxy_primary=proxy_primary,
                                proxy_fallback=proxy_fallback,
                                api_id=api_id,
                                api_hash=api_hash,
                                device_model=device_model,
                                system_version=system_version,
                                app_version=app_version,
                                lang_code=lang_code,
                                system_lang_code=system_lang_code,
                                lang_pack=lang_pack,
                                is_restart=True,
                            )
                            spawned = True
                        except Exception:
                            log.exception(
                                "worker account=%s: restart failed", account_id,
                            )

                if restart_attempted and spawned:
                    # Прозрачное восстановление: публикуем worker.restarted,
                    # а НЕ worker.crashed. worker.crashed заставил бы
                    # auto-runner посчитать воркер офлайн и навсегда
                    # остановить автодиалоги, хотя воркер снова RUNNING.
                    await self._publish_restarted(account_id, str(e))
                    return

                # Финальный крах: рестарт не планировался (повторный крах после
                # авторестарта) или сам рестарт не удался. Здесь auto-runner
                # обязан остановиться — публикуем worker.crashed.
                await self._set_status(account_id, STATUS_CRASHED, error=str(e))
                await self._publish_crashed(account_id, str(e))
            finally:
                # Удаляем слот только если это финальное завершение
                # (при авторестарте мы уже подставили новый слот выше)
                slot = self._slots.get(account_id)
                if slot and slot.worker is worker:
                    self._slots.pop(account_id, None)

        task = asyncio.create_task(_runner())

        owned_slot = _Slot(
            worker=worker,
            task=task,
            started_at=time.monotonic(),
            restart_count=1 if is_restart else 0,
        )
        self._slots[account_id] = owned_slot

        # Статус RUNNING писать здесь НЕЛЬЗЯ — connect ещё не отработал,
        # а get_wrapper()/UI решат что воркер живой (сценарий #1 из аудита
        # рассинхрона). RUNNING ставится из Worker._on_ready-колбэка после
        # успешного connect'а. Здесь остаётся STARTING (был выставлен в
        # start() выше).

    async def _publish_crashed(self, account_id: int, error: str) -> None:
        await bus.publish(
            module=Module.WORKER,
            type=EventType.WORKER_CRASHED,
            status=Status.ERROR,
            account_id=account_id,
            data={"error": error},
        )

    async def _publish_restarted(self, account_id: int, error: str) -> None:
        # Транзиентный крах, восстановленный одним авторестартом. Отдельный
        # тип события: auto-runner его игнорирует (в отличие от worker.crashed),
        # поэтому автодиалоги продолжают работать после прозрачного рестарта.
        await bus.publish(
            module=Module.WORKER,
            type=EventType.WORKER_RESTARTED,
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
