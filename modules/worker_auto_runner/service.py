"""
WorkerAutoRunnerService — авторежим старта диалогов из очереди юзернеймов.

Модель:
  * Один ряд в worker_auto_runners на каждый аккаунт (см. миграцию 0010).
  * Поле queue — TEXT[] с юзернеймами. Атомарно вынимается первый, цикл
    стартует автодиалог через `autochat_service.create_session`.
  * Pause переживает сам себя — НЕ возобновляем автоматически после
    рестарта `app` (поведение зафиксировано с оператором: рестарт = сброс).
  * Падение/остановка воркера автоматически гасит автомат через подписку
    на шину worker.stopped / worker.crashed.

Принципы проекта (см. docs/README.md):
  - Знаем только о двух соседях: worker_manager (через get_wrapper) и
    autochat (через autochat_service.create_session). Никаких чужих
    таблиц.
  - Лог автомата — события в шине, отдельная таблица не нужна.
  - В таблицу worker_auto_runners пишет только этот модуль.

См. docs/dev_diary/9.22_worker_auto_runner.md.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Callable

from core import bus, db
from core.events import EventType, Module, Status

log = logging.getLogger(__name__)

AUTO_RUNNER_GROUP = "auto-runner-worker"
AUTO_RUNNER_CONSUMER = "auto-runner-worker-1"

# Дефолты при первом обращении к воркеру у которого ещё нет ряда.
DEFAULT_INTERVAL_SEC = 1200    # 20 минут между стартами
DEFAULT_JITTER_SEC = 300       # ±5 минут разброс
# Жёсткая нижняя граница чтобы случайно не вписать interval=0 и не зафлудить.
MIN_INTERVAL_SEC = 60
MAX_QUEUE_LEN = 1000

# На сколько максимум разбиваем сон между тиками — чтобы быстро реагировать
# на cancel/stop и проверять что воркер ещё жив, не дожидаясь всей паузы.
_SLEEP_CHUNK_SEC = 5


class AutoRunnerError(Exception):
    code = "AUTO_RUNNER_ERROR"
    http_status = 400


class AccountNotFound(AutoRunnerError):
    code = "ACCOUNT_NOT_FOUND"
    http_status = 404


class WorkerNotRunning(AutoRunnerError):
    code = "WORKER_NOT_RUNNING"
    http_status = 409


class QueueTooLong(AutoRunnerError):
    code = "QUEUE_TOO_LONG"


class InvalidInterval(AutoRunnerError):
    code = "INVALID_INTERVAL"


# ─────────────────────────────────────────────────────────────────────
# SQL-хелперы (этот модуль — единственный кто пишет в worker_auto_runners)
# ─────────────────────────────────────────────────────────────────────

async def _ensure_row(account_id: int) -> None:
    """Создать ряд с дефолтами, если его ещё нет. Идемпотентно."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO worker_auto_runners (account_id)
            VALUES ($1)
            ON CONFLICT (account_id) DO NOTHING
            """,
            account_id,
        )


async def _account_exists(account_id: int) -> bool:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT 1 FROM accounts WHERE id = $1", account_id,
        ))


async def _select_one(account_id: int) -> dict[str, Any] | None:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT account_id, is_running, queue, interval_sec, jitter_sec,
                   last_action_at, last_status, updated_at
            FROM worker_auto_runners
            WHERE account_id = $1
            """,
            account_id,
        )
    return dict(row) if row else None


async def _pop_first_username(account_id: int) -> str | None:
    """
    Атомарно достать и удалить первый юзернейм из очереди.
    None если очередь пуста или ряда нет.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT queue FROM worker_auto_runners WHERE account_id = $1 FOR UPDATE",
                account_id,
            )
            if row is None:
                return None
            q: list[str] = list(row["queue"] or [])
            if not q:
                return None
            head = q[0]
            tail = q[1:]
            await conn.execute(
                """
                UPDATE worker_auto_runners
                SET queue = $2,
                    last_action_at = NOW(),
                    updated_at = NOW()
                WHERE account_id = $1
                """,
                account_id, tail,
            )
    return head


async def _set_running(account_id: int, is_running: bool, last_status: str | None = None) -> None:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE worker_auto_runners
            SET is_running = $2,
                last_status = COALESCE($3, last_status),
                updated_at = NOW()
            WHERE account_id = $1
            """,
            account_id, is_running, last_status,
        )


# ─────────────────────────────────────────────────────────────────────
# Public API класса
# ─────────────────────────────────────────────────────────────────────

class WorkerAutoRunnerService:
    """
    Создаётся в lifespan один на приложение. `run()` поднимается как
    asyncio.Task — фоновый consumer шины (ловит worker.stopped/crashed
    чтобы гасить runner'ы соответствующих аккаунтов).
    """

    def __init__(
        self,
        *,
        get_wrapper: Callable[[int], Any],
        autochat_service: Any,
    ) -> None:
        self._get_wrapper = get_wrapper
        self._autochat = autochat_service
        # Активные циклы — account_id → Task. Только тут ими управляем.
        self._tasks: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

    # ─── Lifecycle (вызывается из api/main.py lifespan) ───────────────

    async def reset_on_boot(self) -> None:
        """
        Сбросить флаг is_running у всех — после рестарта приложения
        автомат не возобновляется автоматически (договорённость с
        оператором). Если кто-то висел is_running=true со времени до
        рестарта — это рассинхрон, который мы здесь правим.
        """
        pool = db.get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE worker_auto_runners
                SET is_running = false,
                    last_status = 'reset on app restart',
                    updated_at = NOW()
                WHERE is_running = true
                """,
            )
        # asyncpg execute() возвращает строку вида 'UPDATE N'.
        log.info("auto_runner: reset_on_boot done (%s)", result)

    async def run(self) -> None:
        """
        Фоновый consumer шины: ловит worker.stopped / worker.crashed,
        гасит соответствующего runner'a.
        """
        await bus.ensure_group(AUTO_RUNNER_GROUP)
        log.info("WorkerAutoRunnerService loop started")

        while not self._stop_event.is_set():
            try:
                batch = await bus.read_group(
                    AUTO_RUNNER_GROUP, AUTO_RUNNER_CONSUMER,
                    count=50, block_ms=5000,
                )
                if not batch:
                    continue
                ack_ids: list[str] = []
                for stream_id, event in batch:
                    try:
                        await self._dispatch_bus_event(event)
                        ack_ids.append(stream_id)
                        await bus.record_success(AUTO_RUNNER_GROUP, stream_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.exception(
                            "auto_runner: dispatch failed for %s",
                            event.get("id"),
                        )
                        force_ack = await bus.record_failure(
                            AUTO_RUNNER_GROUP, stream_id, event, e,
                        )
                        if force_ack:
                            ack_ids.append(stream_id)
                if ack_ids:
                    await bus.ack_group(AUTO_RUNNER_GROUP, ack_ids)
            except asyncio.CancelledError:
                log.info("WorkerAutoRunnerService loop cancelled")
                raise
            except Exception:
                log.exception("WorkerAutoRunnerService loop error")
                await asyncio.sleep(1)

    async def shutdown(self) -> None:
        """Тормозим всех runner'ов при выключении app. Флаг is_running
        НЕ сбрасываем — это делает reset_on_boot при следующем старте."""
        self._stop_event.set()
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=5)
            except Exception:
                pass

    async def _dispatch_bus_event(self, event: dict[str, Any]) -> None:
        et = event.get("type")
        if et not in (EventType.WORKER_STOPPED, EventType.WORKER_CRASHED):
            return
        acc = event.get("account_id")
        if acc is None:
            return
        account_id = int(acc)

        # Сужаем работу: если ряда вовсе нет или флаг уже false и
        # таска не зарегистрирована — просто игнорируем событие.
        existing = await _select_one(account_id)
        if existing is None:
            return
        if not existing["is_running"] and account_id not in self._tasks:
            return

        try:
            await self.stop(account_id, reason="worker_offline")
        except AutoRunnerError:
            pass

    # ─── CRUD ────────────────────────────────────────────────────────

    async def get_config(self, account_id: int) -> dict[str, Any]:
        if not await _account_exists(account_id):
            raise AccountNotFound()
        await _ensure_row(account_id)
        row = await _select_one(account_id)
        assert row is not None
        return row

    async def update_config(
        self,
        account_id: int,
        *,
        queue: list[str] | None = None,
        interval_sec: int | None = None,
        jitter_sec: int | None = None,
    ) -> dict[str, Any]:
        if not await _account_exists(account_id):
            raise AccountNotFound()

        if interval_sec is not None and interval_sec < MIN_INTERVAL_SEC:
            raise InvalidInterval(f"interval_sec must be >= {MIN_INTERVAL_SEC}")
        if jitter_sec is not None and jitter_sec < 0:
            raise InvalidInterval("jitter_sec must be >= 0")
        if queue is not None:
            cleaned = [self._normalize_username(u) for u in queue]
            cleaned = [u for u in cleaned if u]
            if len(cleaned) > MAX_QUEUE_LEN:
                raise QueueTooLong(f"queue length must be <= {MAX_QUEUE_LEN}")
            queue = cleaned

        await _ensure_row(account_id)
        sets: list[str] = ["updated_at = NOW()"]
        params: list[Any] = [account_id]
        if queue is not None:
            params.append(queue)
            sets.append(f"queue = ${len(params)}")
        if interval_sec is not None:
            params.append(int(interval_sec))
            sets.append(f"interval_sec = ${len(params)}")
        if jitter_sec is not None:
            params.append(int(jitter_sec))
            sets.append(f"jitter_sec = ${len(params)}")

        pool = db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE worker_auto_runners SET {', '.join(sets)} WHERE account_id = $1",
                *params,
            )
        return await self.get_config(account_id)

    # ─── Start / Stop ────────────────────────────────────────────────

    async def start(self, account_id: int) -> dict[str, Any]:
        if not await _account_exists(account_id):
            raise AccountNotFound()
        # Воркер должен быть жив. Иначе start не имеет смысла.
        if self._get_wrapper(account_id) is None:
            raise WorkerNotRunning()
        await _ensure_row(account_id)

        async with self._lock:
            existing = self._tasks.get(account_id)
            if existing is not None and not existing.done():
                # Уже бежит — идемпотентно.
                return await self.get_config(account_id)

            await _set_running(account_id, True, last_status="started")
            task = asyncio.create_task(
                self._account_loop(account_id),
                name=f"auto-runner-{account_id}",
            )
            self._tasks[account_id] = task

        await bus.publish(
            module=Module.WORKER_AUTO_RUNNER,
            type=EventType.AUTO_RUNNER_STARTED,
            status=Status.SUCCESS,
            account_id=account_id,
            data={"reason": "manual"},
        )
        return await self.get_config(account_id)

    async def stop(self, account_id: int, *, reason: str = "manual") -> dict[str, Any]:
        async with self._lock:
            task = self._tasks.pop(account_id, None)

        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:
                pass

        # Идемпотентность: если ряда нет — создаём дефолтный и возвращаем,
        # ничего в шину не публикуем (нет «остановки» того, чего не было).
        await _ensure_row(account_id)
        existing = await _select_one(account_id)
        assert existing is not None

        # Если уже было is_running=false и не было живой таски — тоже no-op,
        # без фантомных AUTO_RUNNER_STOPPED.
        if not existing["is_running"] and task is None:
            return existing

        await _set_running(account_id, False, last_status=f"stopped ({reason})")
        await bus.publish(
            module=Module.WORKER_AUTO_RUNNER,
            type=EventType.AUTO_RUNNER_STOPPED,
            status=Status.SUCCESS,
            account_id=account_id,
            data={"reason": reason},
        )
        return await self.get_config(account_id)

    # ─── Цикл одного автомата ────────────────────────────────────────

    async def _account_loop(self, account_id: int) -> None:
        """
        Один проход:
          1. Проверка что воркер жив (иначе stop).
          2. Pop первого юзернейма из очереди.
             - Пустая очередь → AUTO_RUNNER_QUEUE_EMPTY, ждём interval.
          3. Вызов autochat.create_session.
             - Успех → AUTO_RUNNER_LEAD_SENT.
             - UsernameNotFound / прочие ошибки → AUTO_RUNNER_LEAD_SKIPPED,
               идём дальше (юзернейм уже удалён из очереди).
          4. Sleep interval + random(-jitter, +jitter), но не меньше
             MIN_INTERVAL_SEC. Сон бьём чанками чтобы быстрее реагировать
             на cancel/смерть воркера.
        """
        try:
            while True:
                # 1. Жив ли воркер?
                if self._get_wrapper(account_id) is None:
                    log.info(
                        "auto_runner[%s]: worker is offline, stopping",
                        account_id,
                    )
                    await _set_running(account_id, False, last_status="worker offline")
                    await bus.publish(
                        module=Module.WORKER_AUTO_RUNNER,
                        type=EventType.AUTO_RUNNER_STOPPED,
                        status=Status.SUCCESS,
                        account_id=account_id,
                        data={"reason": "worker_offline"},
                    )
                    return

                # 2. Pop username.
                username = await _pop_first_username(account_id)
                if username is None:
                    await bus.publish(
                        module=Module.WORKER_AUTO_RUNNER,
                        type=EventType.AUTO_RUNNER_QUEUE_EMPTY,
                        status=Status.SUCCESS,
                        account_id=account_id,
                    )
                else:
                    await self._try_start_dialog(account_id, username)

                # 3. Sleep.
                cfg = await _select_one(account_id)
                if cfg is None:
                    return
                interval = max(int(cfg["interval_sec"]), MIN_INTERVAL_SEC)
                jitter = max(int(cfg["jitter_sec"]), 0)
                delay = interval + random.uniform(-jitter, jitter)
                delay = max(delay, float(MIN_INTERVAL_SEC))
                await self._chunked_sleep(account_id, delay)

        except asyncio.CancelledError:
            log.info("auto_runner[%s]: cancelled", account_id)
            raise
        except Exception:
            log.exception("auto_runner[%s]: loop crashed", account_id)
            await _set_running(account_id, False, last_status="crashed")
            await bus.publish(
                module=Module.WORKER_AUTO_RUNNER,
                type=EventType.AUTO_RUNNER_STOPPED,
                status=Status.ERROR,
                account_id=account_id,
                data={"reason": "loop_crashed"},
            )

    async def _try_start_dialog(self, account_id: int, username: str) -> None:
        """Вызывает autochat.create_session. Любую ошибку превращает в SKIPPED."""
        try:
            session = await self._autochat.create_session(
                account_id=account_id,
                username=username,
            )
            await bus.publish(
                module=Module.WORKER_AUTO_RUNNER,
                type=EventType.AUTO_RUNNER_LEAD_SENT,
                status=Status.SUCCESS,
                account_id=account_id,
                data={
                    "username": username,
                    "session_id": session.get("id") if isinstance(session, dict) else None,
                },
            )
        except Exception as e:
            log.warning(
                "auto_runner[%s]: start_dialog skipped username=%s err=%s",
                account_id, username, e,
            )
            await bus.publish(
                module=Module.WORKER_AUTO_RUNNER,
                type=EventType.AUTO_RUNNER_LEAD_SKIPPED,
                status=Status.ERROR,
                account_id=account_id,
                data={"username": username, "error": str(e)[:300]},
            )

    async def _chunked_sleep(self, account_id: int, delay: float) -> None:
        """
        Спим короткими чанками, проверяя жив ли воркер. Это уменьшает
        задержку реакции на disconnect воркера без подписки на отдельный
        канал.
        """
        elapsed = 0.0
        while elapsed < delay:
            chunk = min(_SLEEP_CHUNK_SEC, delay - elapsed)
            await asyncio.sleep(chunk)
            elapsed += chunk
            if self._get_wrapper(account_id) is None:
                # Сам цикл на следующей итерации поймает это и завершится.
                return

    # ─── Утилиты ─────────────────────────────────────────────────────

    @staticmethod
    def _normalize_username(raw: str) -> str:
        """Чистим строку, убираем @ если есть, обрезаем пробелы."""
        if not isinstance(raw, str):
            return ""
        u = raw.strip()
        if u.startswith("@"):
            u = u[1:]
        return u
