"""
WorkerAutoRunnerService — авторежим старта диалогов по расписанию окна.

Модель (миграция 0014):
  * Сессия автопрописи — фиксированное окно `window_hours` часов (дефолт 12).
    В начале окна выбирается N лидов (N ∈ [min_leads, max_leads]),
    их timestamps распределяются по окну случайно (uniform); первый лид —
    не раньше чем через `warmup_minutes` (дефолт 10) после `start()`.
  * `schedule TIMESTAMPTZ[]` в БД — оставшиеся моменты. По мере обработки
    первого элемента моменты удаляются (persist, переживает рестарт app).
  * Когда `schedule=[]` или `NOW() >= session_ends_at` → авто-стоп с
    reason=`session_completed`.
  * После рестарта app `reset_on_boot` НЕ гасит is_running:
    - Сессии с `session_ends_at <= NOW()` → is_running=false (мертвы).
    - Сессии с `session_ends_at >  NOW()` → остаются is_running=true, а
      `resume_running_loops()` поднимает их loop-task'и на оставшемся
      расписании.
  * Падение/остановка воркера гасит автомат через шину worker.stopped /
    worker.crashed.

Принципы проекта (см. docs/PROJECT_SPEC.md):
  - Знаем только о двух соседях: worker_manager (через get_wrapper) и
    autochat (через autochat_service.create_session). Никаких чужих
    таблиц.
  - Лог автомата — события в шине, отдельная таблица не нужна.
  - В таблицу worker_auto_runners пишет только этот модуль.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from core import bus, db
from core.events import EventType, Module, Status

log = logging.getLogger(__name__)

AUTO_RUNNER_GROUP = "auto-runner-worker"
AUTO_RUNNER_CONSUMER = "auto-runner-worker-1"

DEFAULT_WINDOW_HOURS = 12
DEFAULT_MIN_LEADS = 15
# L2 (2026-07-13): синхронизировано с дефолтом БД (миграция 0017 = 18).
# Раньше здесь было 25 — при отсутствии строки в БД update_config брал
# кодовый max=25 и мог поймать `min(=18 из БД) <= max(=25)` рассинхрон.
DEFAULT_MAX_LEADS = 18
DEFAULT_WARMUP_MINUTES = 10

MAX_WINDOW_HOURS = 48
MAX_LEADS_UPPER = 500
MAX_WARMUP_MINUTES = 240
MAX_QUEUE_LEN = 1000

# Flood-recovery через @SpamBot (Fix #5, 2026-07-11).
# При срабатывании PEER_FLOOD ("Too many requests" без секунд) или
# FloodWaitError у воркера, автоматически:
#   1. Шлём @SpamBot команду /start ДВА раза с паузой ~3 сек.
#      Ответ бота здесь не проверяется, снятие ограничения не гарантируется.
#   2. Ставим `cooldown_until = NOW() + base + rand(0-5 мин)`, где base —
#      положительное число секунд от TG, иначе FLOOD_COOLDOWN_BASE_SEC.
#      `_account_loop` не отправляет ни одного лида пока cooldown не
#      истёк; затем оставшиеся слоты распределяются по оставшемуся окну.
SPAM_BOT_USERNAME = "SpamBot"
SPAM_BOT_START_ATTEMPTS = 2
SPAM_BOT_DELAY_SEC = 3.0

# Fix #12 (2026-07-12): grace period при `get_wrapper()=None`. Worker.run
# теперь сам ретраит connect до 5 попыток на main-прокси, потом свитч на
# fallback (см. worker.py). Пока идут ретраи `get_wrapper()` возвращает
# None (wrapper.is_connected() = False). Раньше auto_runner сразу
# self-stop'ился с "worker offline" — теперь ждём до MAX_WRAPPER_MISSES
# подряд с паузой WRAPPER_MISS_SLEEP_SEC. 3 × 3 мин = 9 мин grace period.
# За это время Worker.run обычно успевает восстановить связь.
MAX_WRAPPER_MISSES = 3
WRAPPER_MISS_SLEEP_SEC = 180
# R-2 (2026-07-13): мягкий потолок ожидания воркера НА СТАРТЕ. Раньше для
# аккаунта, чей воркер так и не поднялся (разлогин / невалидная прокси), петля
# молча ждала до session_ends_at (до 12ч), а UI показывал is_running=true со
# старым last_status='started'. Теперь на каждой итерации пишем честный
# last_status, а после STARTUP_MAX_WAIT_SEC без воркера — честно стопаемся.
STARTUP_MAX_WAIT_SEC = 30 * 60   # ~30 мин (запас на stagger-старт воркеров)

# Базовый cooldown без секунд от TG — 15 мин (Fix #11, 2026-07-12).
# По указанию оператора 2026-10-10 общая случайная добавка сокращена:
# 15 мин + rand(0-5 мин) = 15-20 мин. Общий расчёт для обоих типов сохранён.
FLOOD_COOLDOWN_BASE_SEC = 15 * 60

# Дополнительный «случайный отдых» который прибавляется поверх времени
# запрошенного TG (для FloodWait) или поверх `FLOOD_COOLDOWN_BASE_SEC`
# (для PEER_FLOOD без секунд), распределяет восстановления во времени.
FLOOD_EXTRA_REST_MIN_SEC = 0
FLOOD_EXTRA_REST_MAX_SEC = 5 * 60

# Первый лид ложится в диапазон [warmup_end, warmup_end + FIRST_SPREAD_SEC],
# ограниченный session_ends_at. Это гарантирует что автомат не выстрелит
# прямо в момент включения — но и не будет ждать половину окна впустую.
FIRST_LEAD_SPREAD_SEC = 900  # 15 минут

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


class InvalidConfig(AutoRunnerError):
    code = "INVALID_CONFIG"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _cancel_and_wait(task: asyncio.Task, timeout: float = 5) -> None:
    """Отменить task и дождаться завершения, не давая исключению утечь наверх.

    C2 + M6 (2026-07-13): отменённый task бросает ``asyncio.CancelledError``,
    который в Python 3.12+ НЕ наследник ``Exception`` — поэтому голый
    ``except Exception`` его не ловит. Раньше stop()/shutdown() ловили только
    ``Exception`` → CancelledError всплывал: ``/auto-runner/stop`` отдавал 500,
    а в консьюмере (``run``) «спуриозный» CancelledError из dispatch убивал
    наблюдателя офлайн-воркеров навсегда. Единый helper ловит ВСЁ (Cancelled,
    Timeout, прочие) — вызов гарантированно тихий.
    """
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
        pass


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
            SELECT account_id, is_running, queue,
                   window_hours, min_leads, max_leads, warmup_minutes,
                   active_start_hour_msk, active_end_hour_msk,
                   session_started_at, session_ends_at, schedule,
                   cooldown_until, cooldown_reason,
                   last_action_at, last_status, updated_at
            FROM worker_auto_runners
            WHERE account_id = $1
            """,
            account_id,
        )
    return dict(row) if row else None


async def _regenerate_schedule_for_remaining_window(account_id: int) -> int:
    """После истечения cooldown: оставшиеся моменты в `schedule[]` перераспределяем
    равномерно в оставшееся окно активной прописи.

    Причина: пока автомат был в cooldown, оригинальные timestamps в schedule
    "прошли" (реально они уже в прошлом). Если продолжить как есть — loop
    выпалит все просроченные слоты подряд, PEER_FLOOD вернётся сразу.
    Правильно — вычислить сколько лидов ещё осталось (`len(schedule)`) и
    равномерно разбросать их по оставшемуся времени [NOW, session_ends_at].

    Fix #10 (2026-07-11). Возвращает новую длину schedule (для логирования).
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT schedule, session_ends_at FROM worker_auto_runners "
            "WHERE account_id = $1",
            account_id,
        )
        if row is None:
            return 0
        remaining: list[datetime] = list(row["schedule"] or [])
        session_end: datetime | None = row["session_ends_at"]
        now = _utcnow()
        if not remaining or session_end is None or now >= session_end:
            return len(remaining)

        n = len(remaining)
        delta = (session_end - now).total_seconds()
        if delta <= 0:
            return n

        offsets = sorted(random.uniform(0.0, delta) for _ in range(n))
        new_schedule = [now + timedelta(seconds=o) for o in offsets]
        await conn.execute(
            "UPDATE worker_auto_runners SET schedule = $2, updated_at = NOW() "
            "WHERE account_id = $1",
            account_id, new_schedule,
        )
        return n


async def _set_cooldown(
    account_id: int, until: datetime | None, reason: str | None,
) -> None:
    """Установить или сбросить cooldown_until/reason."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE worker_auto_runners
            SET cooldown_until  = $2,
                cooldown_reason = $3,
                updated_at      = NOW()
            WHERE account_id = $1
            """,
            account_id, until, reason,
        )


async def _get_account_lead_meta(account_id: int) -> tuple[str, str]:
    """(lead_source, target_gender) — источник лидов и приоритетный пол.

    Дефолты ('manual', 'any') — если по какой-то причине запись в accounts
    исчезла (race с удалением).
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT lead_source, target_gender FROM accounts WHERE id = $1",
            account_id,
        )
    if row is None:
        return "manual", "any"
    return str(row["lead_source"] or "manual"), str(row["target_gender"] or "any")


async def _pop_first_username(account_id: int) -> str | None:
    """Атомарно достать и удалить первый юзернейм из очереди.
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


async def _pop_first_schedule(account_id: int) -> datetime | None:
    """Атомарно достать и удалить первый timestamp из schedule.
    None если пусто.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT schedule FROM worker_auto_runners WHERE account_id = $1 FOR UPDATE",
                account_id,
            )
            if row is None:
                return None
            s: list[datetime] = list(row["schedule"] or [])
            if not s:
                return None
            head = s[0]
            tail = s[1:]
            await conn.execute(
                """
                UPDATE worker_auto_runners
                SET schedule = $2,
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


async def _touch_last_status(account_id: int, status: str) -> None:
    """Обновить last_status БЕЗ переключения is_running.

    Используется чтобы стереть устаревшие "ошибочные" пометки
    (например ``stopped (needs_operator:SESSION_EXPIRED)`` от вчерашнего
    self-stop'а) когда автомат сегодня успешно отправляет лида.
    Без этого UI показывал бы старое состояние навсегда — главный
    источник ложных диагнозов в аудите 2026-07-11.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE worker_auto_runners
            SET last_status = $2,
                last_action_at = NOW(),
                updated_at = NOW()
            WHERE account_id = $1
            """,
            account_id, status,
        )


async def _write_session(
    account_id: int,
    *,
    started_at: datetime,
    ends_at: datetime,
    schedule: list[datetime],
) -> None:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE worker_auto_runners
            SET session_started_at = $2,
                session_ends_at    = $3,
                schedule           = $4,
                updated_at         = NOW()
            WHERE account_id = $1
            """,
            account_id, started_at, ends_at, schedule,
        )


async def _clear_session(account_id: int) -> None:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE worker_auto_runners
            SET schedule           = '{}'::TIMESTAMPTZ[],
                session_started_at = NULL,
                session_ends_at    = NULL,
                updated_at         = NOW()
            WHERE account_id = $1
            """,
            account_id,
        )


# ─────────────────────────────────────────────────────────────────────
# Генератор расписания
# ─────────────────────────────────────────────────────────────────────

MSK_OFFSET = timedelta(hours=3)   # UTC+3, БЕЗ учёта DST — в РФ его нет


def _msk_wall_clock_window(
    now_utc: datetime,
    *,
    active_start_hour_msk: int,
    active_end_hour_msk: int,
) -> tuple[datetime, datetime]:
    """Возвращает (start_utc, end_utc) окна активной прописи на сегодня по
    МСК. Если сейчас уже позже `active_end_hour_msk` (т.е. рабочий день
    закончился), берём завтрашнее окно.

    `active_start` и `active_end` — целые часы по МСК (0..24). Внутри окна
    расписание строится через `_generate_schedule`.
    """
    now_msk = now_utc + MSK_OFFSET
    today_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    start_msk = today_msk.replace(hour=active_start_hour_msk)
    # end_hour может быть 24 → переносится в 00:00 следующего дня.
    if active_end_hour_msk == 24:
        end_msk = (today_msk + timedelta(days=1)).replace(hour=0)
    else:
        end_msk = today_msk.replace(hour=active_end_hour_msk)

    # Уже позже конца окна сегодня → берём завтрашнее.
    if now_msk >= end_msk:
        start_msk += timedelta(days=1)
        end_msk += timedelta(days=1)

    return (start_msk - MSK_OFFSET), (end_msk - MSK_OFFSET)


def _generate_schedule(
    now: datetime,
    *,
    active_start_hour_msk: int,
    active_end_hour_msk: int,
    min_leads: int,
    max_leads: int,
    warmup_minutes: int,
) -> tuple[list[datetime], datetime]:
    """Строим расписание на активное окно (Fix #9, 2026-07-11).

    Окно — wall-clock по МСК [active_start_hour_msk, active_end_hour_msk].
    Если сейчас (`now`, UTC) уже позже `active_end` — окно на завтра.
    Первый лид не раньше `max(now + warmup, window_start)` + случайный
    сдвиг до FIRST_LEAD_SPREAD_SEC. Остальные — uniform в
    [first, window_end], sorted.

    Возвращает (schedule, session_ends_at).
    """
    n = random.randint(min_leads, max_leads)
    window_start, window_end = _msk_wall_clock_window(
        now,
        active_start_hour_msk=active_start_hour_msk,
        active_end_hour_msk=active_end_hour_msk,
    )
    # Первая точка — не раньше warmup от NOW и не раньше открытия окна.
    warmup_end = now + timedelta(minutes=warmup_minutes)
    earliest_first = max(warmup_end, window_start)

    # Если warmup выкатил нас за окно — окно уже пропущено, берём завтра.
    if earliest_first >= window_end:
        window_start = window_start + timedelta(days=1)
        window_end = window_end + timedelta(days=1)
        earliest_first = max(warmup_end, window_start)

    spread_limit = max((window_end - earliest_first).total_seconds(), 1.0)
    first_offset = random.uniform(0, min(float(FIRST_LEAD_SPREAD_SEC), spread_limit))
    first = earliest_first + timedelta(seconds=first_offset)
    if first > window_end:
        first = window_end

    window_seconds = max((window_end - first).total_seconds(), 0.0)
    rest_offsets = sorted(random.uniform(0.0, window_seconds) for _ in range(n - 1))
    rest = [first + timedelta(seconds=o) for o in rest_offsets]

    return [first] + rest, window_end


# ─────────────────────────────────────────────────────────────────────
# Public API класса
# ─────────────────────────────────────────────────────────────────────

class WorkerAutoRunnerService:
    """Создаётся в lifespan один на приложение. `run()` поднимается как
    asyncio.Task — фоновый consumer шины (ловит worker.stopped/crashed
    чтобы гасить runner'ы соответствующих аккаунтов).
    """

    def __init__(
        self,
        *,
        get_wrapper: Callable[[int], Any],
        autochat_service: Any,
        partner_crm_service: Any = None,
    ) -> None:
        self._get_wrapper = get_wrapper
        self._autochat = autochat_service
        # PartnerCrmService — нужен для воркеров с `accounts.lead_source =
        # 'partner_crm'` (берут лидов из общего пула вместо своей очереди).
        # Передаётся опционально, чтобы старые тесты / сценарии без
        # partner_crm работали без изменений.
        self._partner_crm = partner_crm_service
        # Активные циклы — account_id → Task. Только тут ими управляем.
        self._tasks: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        # Самостоп по needs_operator: `_try_start_dialog` возвращает reason
        # значением, `_account_loop` сам выходит через `_do_self_stop` — без
        # долгоживущего словаря-side-channel (убрано 2026-07-13).

    # ─── Lifecycle (вызывается из api/main.py lifespan) ───────────────

    async def reset_on_boot(self) -> None:
        """
        При старте app:
          1. Гасим is_running у тех, у кого сессия истекла пока app лежал
             (session_ends_at is NULL для legacy-строк или уже в прошлом).
          2. Оставшиеся is_running=true — живые сессии, их подхватит
             `resume_running_loops()` (запускается после инициализации
             worker_manager, когда wrapper'ы уже подняты).
        """
        pool = db.get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE worker_auto_runners
                SET is_running = false,
                    last_status = 'session expired during app downtime',
                    updated_at = NOW()
                WHERE is_running = true
                  AND (session_ends_at IS NULL OR session_ends_at <= NOW())
                """,
            )
        log.info("auto_runner: reset_on_boot done (%s)", result)

    async def resume_running_loops(self) -> None:
        """Поднять loop-task'и для всех is_running=true с живой сессией.

        Вызывается ПОСЛЕ инициализации worker_manager, чтобы к моменту
        запуска loop'а wrapper'ы уже были доступны через get_wrapper.
        """
        pool = db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT account_id FROM worker_auto_runners
                WHERE is_running = true AND session_ends_at > NOW()
                """,
            )
        if not rows:
            return

        async with self._lock:
            resumed = 0
            for row in rows:
                acc = int(row["account_id"])
                if acc in self._tasks and not self._tasks[acc].done():
                    continue
                # M5 (2026-07-13): пока app лежал, метки в `schedule[]` ушли
                # в прошлое. Без пересборки loop выпалит все просроченные
                # слоты подряд на первых итерациях → PEER_FLOOD сразу после
                # рестарта. Растягиваем оставшиеся лиды равномерно по остатку
                # окна [NOW, session_ends_at] один раз при возобновлении.
                try:
                    await _regenerate_schedule_for_remaining_window(acc)
                except Exception:
                    log.exception(
                        "auto_runner: resume regenerate schedule failed acc=%s",
                        acc,
                    )
                # Не проверяем get_wrapper здесь: если воркера ещё нет, цикл
                # сам себя погасит на первой же итерации (см. `_account_loop`).
                task = asyncio.create_task(
                    self._account_loop(acc), name=f"auto-runner-{acc}-resumed",
                )
                self._tasks[acc] = task
                resumed += 1
        log.info("auto_runner: resumed %d loops from persisted schedule", resumed)

    async def run(self) -> None:
        """Фоновый consumer шины: ловит worker.stopped / worker.crashed,
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
                        # M6 (2026-07-13): различаем реальную отмену самого
                        # консьюмера (выставлен _stop_event) и «спуриозный»
                        # CancelledError, всплывший из под-операции (напр.
                        # cancel дочернего loop-task в stop()). Последний НЕ
                        # должен убивать наблюдателя офлайн-воркеров.
                        if self._stop_event.is_set():
                            raise
                        log.warning(
                            "auto_runner: спуриозный CancelledError из dispatch "
                            "(event=%s), продолжаем", event.get("id"),
                        )
                        ack_ids.append(stream_id)
                        continue
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
        НЕ сбрасываем — расписание в БД, при следующем старте app
        сессия подхватится (если session_ends_at ещё не прошла)."""
        self._stop_event.set()
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        # C2/M6: единый безопасный helper — CancelledError не утечёт наверх.
        await asyncio.gather(*(_cancel_and_wait(t) for t in tasks))

    async def _dispatch_bus_event(self, event: dict[str, Any]) -> None:
        et = event.get("type")
        if et not in (EventType.WORKER_STOPPED, EventType.WORKER_CRASHED):
            return
        acc = event.get("account_id")
        if acc is None:
            return
        account_id = int(acc)

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
        window_hours: int | None = None,
        min_leads: int | None = None,
        max_leads: int | None = None,
        warmup_minutes: int | None = None,
        active_start_hour_msk: int | None = None,
        active_end_hour_msk: int | None = None,
    ) -> dict[str, Any]:
        if not await _account_exists(account_id):
            raise AccountNotFound()

        # Валидация: и по одному полю, и по паре min/max (согласованно).
        if window_hours is not None and not (1 <= window_hours <= MAX_WINDOW_HOURS):
            raise InvalidConfig(f"window_hours must be in [1, {MAX_WINDOW_HOURS}]")
        if min_leads is not None and min_leads < 1:
            raise InvalidConfig("min_leads must be >= 1")
        if max_leads is not None and max_leads > MAX_LEADS_UPPER:
            raise InvalidConfig(f"max_leads must be <= {MAX_LEADS_UPPER}")
        if warmup_minutes is not None and not (0 <= warmup_minutes <= MAX_WARMUP_MINUTES):
            raise InvalidConfig(
                f"warmup_minutes must be in [0, {MAX_WARMUP_MINUTES}]"
            )
        # Fix #9: часы дня по МСК (0..23 start, 1..24 end, end > start).
        if active_start_hour_msk is not None and not (0 <= active_start_hour_msk <= 23):
            raise InvalidConfig("active_start_hour_msk must be in [0, 23]")
        if active_end_hour_msk is not None and not (1 <= active_end_hour_msk <= 24):
            raise InvalidConfig("active_end_hour_msk must be in [1, 24]")
        if active_start_hour_msk is not None or active_end_hour_msk is not None:
            existing = await _select_one(account_id)
            eff_start = active_start_hour_msk if active_start_hour_msk is not None else (
                int(existing["active_start_hour_msk"]) if existing else 8
            )
            eff_end = active_end_hour_msk if active_end_hour_msk is not None else (
                int(existing["active_end_hour_msk"]) if existing else 23
            )
            if eff_end <= eff_start:
                raise InvalidConfig("active_end_hour_msk must be > active_start_hour_msk")
        # min <= max: проверяем на комбинации новых значений + текущих в БД.
        if min_leads is not None or max_leads is not None:
            existing = await _select_one(account_id)
            eff_min = min_leads if min_leads is not None else (
                int(existing["min_leads"]) if existing else DEFAULT_MIN_LEADS
            )
            eff_max = max_leads if max_leads is not None else (
                int(existing["max_leads"]) if existing else DEFAULT_MAX_LEADS
            )
            if eff_min > eff_max:
                raise InvalidConfig("min_leads must be <= max_leads")

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
        if window_hours is not None:
            params.append(int(window_hours))
            sets.append(f"window_hours = ${len(params)}")
        if min_leads is not None:
            params.append(int(min_leads))
            sets.append(f"min_leads = ${len(params)}")
        if max_leads is not None:
            params.append(int(max_leads))
            sets.append(f"max_leads = ${len(params)}")
        if warmup_minutes is not None:
            params.append(int(warmup_minutes))
            sets.append(f"warmup_minutes = ${len(params)}")
        if active_start_hour_msk is not None:
            params.append(int(active_start_hour_msk))
            sets.append(f"active_start_hour_msk = ${len(params)}")
        if active_end_hour_msk is not None:
            params.append(int(active_end_hour_msk))
            sets.append(f"active_end_hour_msk = ${len(params)}")

        pool = db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE worker_auto_runners SET {', '.join(sets)} WHERE account_id = $1",
                *params,
            )
        return await self.get_config(account_id)

    # ─── Start / Stop ────────────────────────────────────────────────

    async def start(self, account_id: int) -> dict[str, Any]:
        """Запустить сессию автопрописи.

        Читает текущий конфиг (window_hours/min/max_leads/warmup), генерирует
        свежее расписание, стартует loop-task. Если сессия уже идёт (task
        живой) — идемпотентно возвращает текущее состояние без пересборки
        расписания.
        """
        if not await _account_exists(account_id):
            raise AccountNotFound()
        if self._get_wrapper(account_id) is None:
            raise WorkerNotRunning()
        await _ensure_row(account_id)

        async with self._lock:
            existing_task = self._tasks.get(account_id)
            if existing_task is not None and not existing_task.done():
                return await self.get_config(account_id)

            cfg = await _select_one(account_id)
            assert cfg is not None

            now = _utcnow()
            schedule, ends_at = _generate_schedule(
                now,
                active_start_hour_msk=int(cfg["active_start_hour_msk"]),
                active_end_hour_msk=int(cfg["active_end_hour_msk"]),
                min_leads=int(cfg["min_leads"]),
                max_leads=int(cfg["max_leads"]),
                warmup_minutes=int(cfg["warmup_minutes"]),
            )
            await _write_session(
                account_id, started_at=now, ends_at=ends_at, schedule=schedule,
            )
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
            data={
                "reason": "manual",
                "leads_planned": len(schedule),
                "active_start_hour_msk": int(cfg["active_start_hour_msk"]),
                "active_end_hour_msk": int(cfg["active_end_hour_msk"]),
                "session_ends_at": ends_at.isoformat(),
            },
        )
        return await self.get_config(account_id)

    async def stop(self, account_id: int, *, reason: str = "manual") -> dict[str, Any]:
        async with self._lock:
            task = self._tasks.pop(account_id, None)

        if task is not None and not task.done():
            await _cancel_and_wait(task)

        await _ensure_row(account_id)
        existing = await _select_one(account_id)
        assert existing is not None

        if not existing["is_running"] and task is None:
            return existing

        await _set_running(account_id, False, last_status=f"stopped ({reason})")
        await _clear_session(account_id)
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
        """Один проход:
          1. Проверка что воркер жив (иначе self-stop).
          2. Проверка что сессия не истекла (session_ends_at > NOW()).
          3. Read schedule[0]; если пусто → self-stop reason=session_completed.
          4. Sleep до schedule[0] (чанками, реагируя на cancel/worker_offline).
             На выходе — либо пришло время, либо воркер офлайн → следующая
             итерация обработает.
          5. Pop schedule[0], pop username из queue / claim из CRM.
             Пусто → AUTO_RUNNER_QUEUE_EMPTY, момент считается использованным
             (расписание не «отматывается» назад — как и хотел оператор).
          6. autochat.create_session (успех/ошибка → LEAD_SENT/LEAD_SKIPPED).
        """
        wrapper_miss_count = 0   # Fix #12: grace period tracker
        ever_saw_wrapper = False  # Fix #18: startup-race guard
        startup_miss = 0          # R-2: сколько тиков ждём воркера на старте
        try:
            while True:
                # 0. Свежий конфиг + ЕСТЕСТВЕННЫЙ CAP по сроку сессии.
                # M4 (2026-07-13): проверяем `session_ends_at` ДО ожидания
                # воркера. Раньше при `ever_saw_wrapper=False` ветка «ждём
                # wrapper» делала sleep+continue безусловно, не доходя до
                # проверки срока (она была ниже). Для разлогиненного аккаунта
                # wrapper не появлялся никогда → петля крутилась вечно,
                # `is_running` залипал в true. Теперь срок сессии — жёсткий
                # cap на бесконечное ожидание.
                cfg = await _select_one(account_id)
                if cfg is None:
                    return

                schedule: list[datetime] = list(cfg.get("schedule") or [])
                ends_at: datetime | None = cfg.get("session_ends_at")
                cooldown_until: datetime | None = cfg.get("cooldown_until")

                if ends_at is not None and _utcnow() >= ends_at:
                    log.info(
                        "auto_runner[%s]: session window elapsed "
                        "(ends_at=%s, schedule_left=%d), stopping",
                        account_id, ends_at, len(schedule),
                    )
                    await self._do_self_stop(account_id, "session_completed")
                    return

                # 1. Жив ли воркер? (Fix #12 grace period + Fix #18 startup race)
                # Worker.run сам ретраит connect (main → fallback → probe).
                # Пока идёт reconnect, get_wrapper() возвращает None. Даём
                # до MAX_WRAPPER_MISSES × WRAPPER_MISS_SLEEP_SEC = 9 мин grace,
                # только потом self-stop.
                #
                # Fix #18 (2026-07-13): grace-таймер стартует ТОЛЬКО после
                # того как wrapper хоть раз был виден живым. Раньше при
                # рестарте app `resume_running_loops` создавал loop-task'и
                # в lifespan startup, а `worker_manager.start()` поднимал
                # wrapper'ы позже (через stagger-скрипт оператора) — воркеры
                # на хвосте очереди не успевали за grace (9 мин) и автоматы
                # гасли с "worker offline (extended)". Теперь: пока не видели
                # wrapper'а — просто ждём молча (cap выше по session_ends_at).
                if self._get_wrapper(account_id) is None:
                    if not ever_saw_wrapper:
                        startup_miss += 1
                        # R-2 (2026-07-13): пишем ЧЕСТНЫЙ last_status в БД, чтобы
                        # UI показывал «ждём воркера», а не залипшее running/
                        # started. Раньше эта ветка молчала до session_ends_at.
                        await _touch_last_status(
                            account_id, "ожидание воркера (запуск)",
                        )
                        # Мягкий cap: если воркер так и не поднялся за ~30 мин —
                        # честно стопаемся (разлогин / невалидная прокси /
                        # оператор не стартовал воркер), а не крутимся молча.
                        if startup_miss * WRAPPER_MISS_SLEEP_SEC >= STARTUP_MAX_WAIT_SEC:
                            log.info(
                                "auto_runner[%s]: wrapper not up after ~%dм at "
                                "startup, stopping",
                                account_id, STARTUP_MAX_WAIT_SEC // 60,
                            )
                            await _set_running(
                                account_id, False,
                                last_status="воркер не поднялся (запуск)",
                            )
                            await bus.publish(
                                module=Module.WORKER_AUTO_RUNNER,
                                type=EventType.AUTO_RUNNER_STOPPED,
                                status=Status.SUCCESS,
                                account_id=account_id,
                                data={
                                    "reason": "worker_offline_startup",
                                    "waited_sec": startup_miss * WRAPPER_MISS_SLEEP_SEC,
                                },
                            )
                            return
                        log.info(
                            "auto_runner[%s]: waiting for wrapper (startup, %d)",
                            account_id, startup_miss,
                        )
                        await asyncio.sleep(WRAPPER_MISS_SLEEP_SEC)
                        continue
                    wrapper_miss_count += 1
                    if wrapper_miss_count >= MAX_WRAPPER_MISSES:
                        log.info(
                            "auto_runner[%s]: worker offline for %d consecutive "
                            "checks (~%ds), stopping",
                            account_id, wrapper_miss_count,
                            wrapper_miss_count * WRAPPER_MISS_SLEEP_SEC,
                        )
                        await _set_running(
                            account_id, False,
                            last_status="worker offline (extended)",
                        )
                        await bus.publish(
                            module=Module.WORKER_AUTO_RUNNER,
                            type=EventType.AUTO_RUNNER_STOPPED,
                            status=Status.SUCCESS,
                            account_id=account_id,
                            data={
                                "reason": "worker_offline",
                                "miss_count": wrapper_miss_count,
                            },
                        )
                        return
                    log.info(
                        "auto_runner[%s]: worker offline (miss %d/%d), "
                        "waiting %ds before retry",
                        account_id, wrapper_miss_count, MAX_WRAPPER_MISSES,
                        WRAPPER_MISS_SLEEP_SEC,
                    )
                    await asyncio.sleep(WRAPPER_MISS_SLEEP_SEC)
                    continue
                # Wrapper есть — фиксируем «первое появление» и сбрасываем счётчик.
                ever_saw_wrapper = True
                wrapper_miss_count = 0

                # 2/3. Расписание пусто → session_completed (окно ещё не истекло,
                # но лидов больше не осталось).
                if not schedule:
                    log.info(
                        "auto_runner[%s]: schedule empty, session completed "
                        "(ends_at=%s)",
                        account_id, ends_at,
                    )
                    await self._do_self_stop(account_id, "session_completed")
                    return

                # 3.5 (Fix #5). Cooldown после flood/PEER_FLOOD: спим до его
                # истечения, слоты за это время просто пропадают (schedule НЕ
                # пересобирается). После — очищаем cooldown, публикуем
                # cooldown_ended и продолжаем нормально.
                if cooldown_until is not None and _utcnow() < cooldown_until:
                    delay = (cooldown_until - _utcnow()).total_seconds()
                    log.info(
                        "auto_runner[%s]: in cooldown for %.0fs (until %s, reason=%s)",
                        account_id, delay, cooldown_until.isoformat(),
                        cfg.get("cooldown_reason"),
                    )
                    await self._chunked_sleep(account_id, delay)
                    # После сна: воркер мог упасть, cooldown мог быть снят
                    # вручную. Перепроверим на верху.
                    if self._get_wrapper(account_id) is None:
                        continue
                    # Cooldown истёк — чистим, ПЕРЕСОБИРАЕМ расписание из
                    # оставшихся лидов в оставшееся окно (Fix #10) и публикуем
                    # событие "ожил".
                    await _set_cooldown(account_id, None, None)
                    remaining_n = await _regenerate_schedule_for_remaining_window(
                        account_id,
                    )
                    await _touch_last_status(account_id, "running")
                    await bus.publish(
                        module=Module.WORKER_AUTO_RUNNER,
                        type=EventType.AUTO_RUNNER_LEAD_SENT,  # переиспользуем — оператор увидит "ожил"
                        status=Status.SUCCESS,
                        account_id=account_id,
                        data={
                            "flood_recovery_ended": True,
                            "schedule_regenerated_leads": remaining_n,
                        },
                    )
                    log.info(
                        "auto_runner[%s]: cooldown ended, "
                        "schedule regenerated for %d remaining leads",
                        account_id, remaining_n,
                    )
                    continue

                # 4. Sleep до ближайшего момента.
                next_at = schedule[0]
                delay = (next_at - _utcnow()).total_seconds()
                if delay > 0:
                    await self._chunked_sleep(account_id, delay)
                    # После сна: воркер мог упасть — перепроверим на верху.
                    if self._get_wrapper(account_id) is None:
                        continue

                # 5. Атомарно снять momент (persist).
                popped = await _pop_first_schedule(account_id)
                if popped is None:
                    # Кто-то параллельно опустошил schedule (или мы уже
                    # прошли self-stop) — просто на следующую итерацию.
                    continue

                # Определяем источник лида.
                lead_source, target_gender = await _get_account_lead_meta(account_id)
                claimed: dict[str, Any] | None = None
                username: str | None = None
                gender: str | None = None
                from_crm = False

                if lead_source == "partner_crm" and self._partner_crm is not None:
                    claimed = await self._partner_crm.claim_lead_for_account(
                        account_id=account_id,
                        target_gender=target_gender,
                    )
                    if claimed is not None:
                        username = claimed["username"]
                        gender = claimed.get("gender")
                        from_crm = True
                else:
                    username = await _pop_first_username(account_id)

                if username is None:
                    await bus.publish(
                        module=Module.WORKER_AUTO_RUNNER,
                        type=EventType.AUTO_RUNNER_QUEUE_EMPTY,
                        status=Status.SUCCESS,
                        account_id=account_id,
                        data={"lead_source": lead_source, "skipped_slot_at": next_at.isoformat()},
                    )
                else:
                    stop_reason = await self._try_start_dialog(
                        account_id, username,
                        gender=gender, from_crm=from_crm,
                    )
                    # 6. needs_operator → самостоп. Reason возвращается прямо
                    # значением (без долгоживущего _self_stop_requested).
                    if stop_reason:
                        await self._do_self_stop(account_id, stop_reason)
                        return

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

    # ─── Таксономия ошибок autochat.create_session ───────────────────
    #
    # Три категории вердикта (см. `_classify_send_error`):
    #
    #   INVALID_LEAD — лид физически недоступен, никакая попытка не
    #       сработает: аккаунт удалён / приватность закрыта / юзернейм
    #       не существует. Для CRM: push terminal_status=invalid.
    #       НЕ останавливаем автомат, продолжаем со следующим слотом.
    #
    #   TRANSIENT_BACKOFF — лид нормальный, но эта попытка не удалась
    #       по причине временного анти-спама TG или сбоя LLM.
    #       Классические кейсы: PEER_FLOOD ("Too many requests"
    #       без секунд), FloodWaitError (с явными секундами),
    #       GENERATION_FAILED. Правильная реакция — пропустить этот
    #       слот, лида вернуть в пул (CRM-лидов), СЛЕДУЮЩИЙ слот
    #       попробовать снова. Автомат остаётся включённым.
    #       Раньше это шло в NEEDS_OPERATOR → self-stop, оператор в
    #       среднем 90% времени тратил на "разбудить" воркеры, а
    #       реального ручного вмешательства не требовалось.
    #
    #   NEEDS_OPERATOR — реально нужна реакция человека: сессия TG
    #       revoke'нута, воркер офлайн, неизвестный код ошибки.
    #       Останавливаем автомат, лида возвращаем в пул.
    _INVALID_LEAD_CODES: frozenset[str] = frozenset({
        "USERNAME_NOT_FOUND",
        "USERNAME_UNAVAILABLE",
        "CANNOT_WRITE",
    })
    _TRANSIENT_BACKOFF_CODES: frozenset[str] = frozenset({
        # Anti-spam TG: PEER_FLOOD (Telethon PeerFloodError → сообщение
        # "Too many requests (caused by SendMessageRequest)" без секунд)
        "SEND_FAILED_TRANSIENT",
        # FloodWait с явными секундами (даёт возможность точно спать)
        "FLOOD_WAIT",
        "FloodWaitError",
        # OpenRouter / LLM времеенно отказал — новая попытка на новом
        # слоте скорее всего пройдёт.
        "GENERATION_FAILED",
    })
    _NEEDS_OPERATOR_CODES: frozenset[str] = frozenset({
        "SESSION_EXPIRED",
        "WORKER_NOT_RUNNING",
        # Всё что не в других категориях — тоже сюда (fallback в _classify).
    })

    @classmethod
    def _classify_send_error(cls, err_code: str) -> str:
        """Мапит error_code autochat.create_session на один из вердиктов:

        - ``"invalid_lead"``   — лид мёртвый, финализировать invalid
        - ``"transient"``      — сбой отправки, продолжать
        - ``"needs_operator"`` — нужна реакция человека, self-stop

        Неизвестные коды = ``needs_operator`` (безопасный дефолт).
        """
        if err_code in cls._INVALID_LEAD_CODES:
            return "invalid_lead"
        if err_code in cls._TRANSIENT_BACKOFF_CODES:
            return "transient"
        return "needs_operator"

    @staticmethod
    def _is_flood_error(err_code: str, err_msg: str) -> bool:
        """True если это TG anti-spam flood (нужно триггерить SpamBot recovery).

        Два кейса:
          * `FLOOD_WAIT` / `FloodWaitError` — TG говорит явные секунды ожидания;
          * `SEND_FAILED_TRANSIENT` c "Too many requests" — PEER_FLOOD,
            тихий anti-spam без секунд, снимается через @SpamBot /start
            (у Premium-аккаунтов).

        Не путать с "The specified user was deleted" (тоже
        `SEND_FAILED_TRANSIENT`, но НЕ flood, а мёртвый лид). Матчим только
        по маркеру "Too many requests".
        """
        if err_code in ("FLOOD_WAIT", "FloodWaitError"):
            return True
        if err_code == "SEND_FAILED_TRANSIENT" and "Too many requests" in err_msg:
            return True
        return False

    async def _trigger_flood_recovery(
        self, account_id: int, err_code: str, err_msg: str,
        *, seconds: int | None = None,
    ) -> None:
        """Автоматика при PEER_FLOOD / FloodWait:

          1. Через wrapper воркера шлём @SpamBot команду `/start` дважды с
             паузой `SPAM_BOT_DELAY_SEC`; ответ бота здесь не проверяется.
          2. Ставим `cooldown_until = NOW() + base + rand(0-5 мин)`:
             - `base = seconds` если TG прислал явные секунды (FloodWait);
             - `base = FLOOD_COOLDOWN_BASE_SEC` (15 мин) если секунд нет (PEER_FLOOD).
             Общая случайная добавка распределяет восстановления во времени.
          3. Публикуем событие `auto_runner.lead_skipped` с `flood_recovery=True`.
          4. Обновляем `last_status='cooldown (flood, until ...)'` для UI.

        После истечения cooldown loop сам пересобирает расписание из
        оставшихся лидов в оставшееся окно (см. `_regenerate_schedule`).
        """
        wrapper = self._get_wrapper(account_id)
        # /start × 2. Ошибки съедаем и логируем — они не должны мешать
        # выставить cooldown (главная цель этой функции).
        spambot_ok = False
        if wrapper is not None:
            try:
                for i in range(SPAM_BOT_START_ATTEMPTS):
                    await wrapper.send_message(SPAM_BOT_USERNAME, "/start")
                    if i + 1 < SPAM_BOT_START_ATTEMPTS:
                        await asyncio.sleep(SPAM_BOT_DELAY_SEC)
                spambot_ok = True
            except Exception as e:
                log.warning(
                    "auto_runner[%s]: SpamBot /start failed (%s: %s); "
                    "cooldown will apply anyway",
                    account_id, type(e).__name__, e,
                )
        else:
            log.warning(
                "auto_runner[%s]: no wrapper, cannot send SpamBot /start; "
                "cooldown will apply anyway",
                account_id,
            )

        # Cooldown в БД: base из TG (если есть секунды) + случайный отдых.
        base_sec = float(seconds) if seconds and seconds > 0 else FLOOD_COOLDOWN_BASE_SEC
        extra_sec = random.uniform(FLOOD_EXTRA_REST_MIN_SEC, FLOOD_EXTRA_REST_MAX_SEC)
        until = _utcnow() + timedelta(seconds=base_sec + extra_sec)
        reason = (
            f"flood:{err_code}:{int(base_sec)}s"
            if seconds and seconds > 0
            else f"flood:{err_code}"
        )
        await _set_cooldown(account_id, until, reason)
        await _touch_last_status(
            account_id, f"cooldown (flood, until {until.isoformat(timespec='minutes')})",
        )

        await bus.publish(
            module=Module.WORKER_AUTO_RUNNER,
            type=EventType.AUTO_RUNNER_LEAD_SKIPPED,  # переиспользуем — оператор увидит в лог-фиде
            status=Status.SUCCESS,
            account_id=account_id,
            data={
                "flood_recovery": True,
                "err_code": err_code,
                "err_snippet": err_msg[:200],
                "spambot_ok": spambot_ok,
                "seconds_from_tg": seconds,
                "extra_rest_sec": int(extra_sec),
                "cooldown_until": until.isoformat(),
            },
        )
        log.info(
            "auto_runner[%s]: flood recovery triggered (code=%s, "
            "tg_seconds=%s, extra_rest=%ds, spambot_ok=%s, cooldown_until=%s)",
            account_id, err_code, seconds, int(extra_sec), spambot_ok,
            until.isoformat(),
        )

    async def _try_start_dialog(
        self,
        account_id: int,
        username: str,
        *,
        gender: str | None = None,
        from_crm: bool = False,
    ) -> str | None:
        """Вызывает autochat.create_session. Поведение при ошибке
        определяется таксономией из ``_classify_send_error``.

        Возвращает reason самостопа (`"needs_operator:<code>"`) если автомат
        надо остановить, иначе `None` (продолжаем). Раньше reason писался в
        долгоживущий словарь `_self_stop_requested` — заменено на прямой
        возврат значением (нет side-channel, нет утечки вердикта за границу
        сессии).

        Таксономия:

        - **Успех** → LEAD_SENT, обычный flow.
        - **INVALID_LEAD** (`CANNOT_WRITE` / `USERNAME_NOT_FOUND` /
          `USERNAME_UNAVAILABLE`) → LEAD_SKIPPED; для CRM-лида пуш
          `terminal_status=invalid`. Автомат ПРОДОЛЖАЕТ работу.
        - **TRANSIENT** (`SEND_FAILED_TRANSIENT`=PEER_FLOOD, `FLOOD_WAIT`,
          `GENERATION_FAILED`) → LEAD_SKIPPED_TRANSIENT; для CRM-лида
          reinsert в пул (другой воркер попробует). Автомат
          ПРОДОЛЖАЕТ работу — следующий слот попробует того же
          или нового лида, PEER_FLOOD часто снимается за 30-60 мин
          самим воркером ничего не делая.
        - **NEEDS_OPERATOR** (`SESSION_EXPIRED`, unknown) → LEAD_SKIPPED;
          для CRM-лида reinsert в пул. Метод возвращает reason самостопа,
          автомат ОСТАНАВЛИВАЕТСЯ, оператор разбирается.

        Раньше все не-INVALID ошибки шли в NEEDS_OPERATOR ⇒ автомат
        сразу глушился. По данным событий 88% таких self-stop'ов
        были на PEER_FLOOD (транзиентный TG-anti-spam), реальной ручной
        работы не требовали. Разделение убрало ложные self-stop'ы —
        см. запись в §20 SPEC от 2026-07-11.

        `/start` в CRM здесь не зовём — он сделан на этапе рефилла пула.
        """
        try:
            session = await self._autochat.create_session(
                account_id=account_id,
                username=username,
                gender=gender,
                from_crm=from_crm,
            )
            await bus.publish(
                module=Module.WORKER_AUTO_RUNNER,
                type=EventType.AUTO_RUNNER_LEAD_SENT,
                status=Status.SUCCESS,
                account_id=account_id,
                data={
                    "username": username,
                    "session_id": session.get("id") if isinstance(session, dict) else None,
                    "from_crm": from_crm,
                    "gender": gender,
                },
            )
            # Автомат работает — стираем возможные устаревшие ошибочные
            # last_status'ы от прошлых self-stop'ов. Без этого UI показывал
            # бы вчерашнюю проблему навсегда (главная жалоба из аудита
            # 2026-07-11: ложные "SESSION_EXPIRED" при живой сессии).
            await _touch_last_status(account_id, "running")
            return
        except Exception as e:
            err_code = getattr(e, "code", None) or type(e).__name__
            err_msg = str(e)[:300]
            # Fix #10: TG в некоторых кейсах прямо говорит "жди N секунд"
            # (FloodWaitError, наш AutoChat.FloodWait). Извлекаем чтобы
            # cooldown был не 2 часа наугад, а ровно `N + rand(10..20)мин`.
            err_seconds: int | None = getattr(e, "seconds", None)
            if err_seconds is not None:
                try:
                    err_seconds = int(err_seconds)
                except (TypeError, ValueError):
                    err_seconds = None
            verdict = self._classify_send_error(err_code)

            # SESSION_ALREADY_ACTIVE (2026-07-13): на паре уже есть активная
            # autochat-сессия — лид уже прописан. Это НЕ вина лида и НЕ повод
            # стопать автомат: пропускаем слот и идём дальше. Лид оставляем как
            # есть (им владеет активная сессия), в пул не возвращаем и invalid
            # не пушим. Без этого benign-условие классифицировалось как
            # needs_operator → self-stop, и аккаунт замолкал целиком (симптом
            # у acc=25 после дубль-claim от реапера).
            # Benign-скипы: лид уже прописан — писать повторно НЕ надо и это НЕ
            # повод стопать автомат или штрафовать лида.
            #   SESSION_ALREADY_ACTIVE — на паре уже есть активная сессия ЭТОГО
            #     аккаунта.
            #   ALREADY_CONTACTED (2026-07-13) — этому человеку (по
            #     telegram_user_id) уже писал ЛЮБОЙ аккаунт: сработал корневой
            #     глобальный дедуп в create_session. Пропускаем слот, лида не
            #     возвращаем в пул и invalid не пушим.
            if err_code in ("SESSION_ALREADY_ACTIVE", "ALREADY_CONTACTED"):
                log.info(
                    "auto_runner[%s]: %s skip slot (%s)",
                    account_id, username, err_code,
                )
                await bus.publish(
                    module=Module.WORKER_AUTO_RUNNER,
                    type=EventType.AUTO_RUNNER_LEAD_SKIPPED,
                    status=Status.SUCCESS,
                    account_id=account_id,
                    data={
                        "username": username,
                        "verdict": "skip_already_active"
                        if err_code == "SESSION_ALREADY_ACTIVE"
                        else "skip_already_contacted",
                        "from_crm": from_crm,
                    },
                )
                return

            log.warning(
                "auto_runner[%s]: start_dialog skipped username=%s code=%s "
                "verdict=%s err=%s",
                account_id, username, err_code, verdict, e,
            )
            await bus.publish(
                module=Module.WORKER_AUTO_RUNNER,
                type=EventType.AUTO_RUNNER_LEAD_SKIPPED,
                status=Status.ERROR,
                account_id=account_id,
                data={
                    "username": username,
                    "error": err_msg,
                    "error_code": err_code,
                    "verdict": verdict,
                    "from_crm": from_crm,
                },
            )

        # Не CRM-лид (ручная очередь) → в CRM ничего не пушим и
        # в пул ничего не возвращаем. Оператор сам разберётся.
        # НО self-stop при NEEDS_OPERATOR всё равно нужен: воркер
        # видимо сломан, дальше долбить его юзернеймами из очереди
        # смысла нет.
        if not from_crm or self._partner_crm is None:
            if verdict == "needs_operator":
                log.info(
                    "auto_runner[%s]: self-stop (needs_operator:%s)",
                    account_id, err_code,
                )
                return f"needs_operator:{err_code}"
            if verdict == "transient" and self._is_flood_error(err_code, err_msg):
                # Даже без CRM: flood от TG всё равно требует cooldown,
                # иначе следующий слот тоже упрётся в PEER_FLOOD.
                try:
                    await self._trigger_flood_recovery(
                        account_id, err_code, err_msg, seconds=err_seconds,
                    )
                except Exception:
                    log.exception(
                        "auto_runner[%s]: flood recovery threw (manual queue), continuing",
                        account_id,
                    )
            return None

        # CRM-лид: диспетчеризация по вердикту.
        if verdict == "invalid_lead":
            # Финализируем лид в CRM как invalid.
            try:
                await self._partner_crm.push_lead_finalized(
                    account_id=account_id,
                    username=username,
                    terminal_status="invalid",
                )
            except Exception as push_err:
                log.warning(
                    "auto_runner[%s]: push invalid failed for %s: %s",
                    account_id, username, push_err,
                )
            return

        # Transient (PEER_FLOOD, FloodWait, сбой LLM) — это ВСЕГДА НАШИ причины
        # (общий api_id → троттлинг TG; недоступность OpenRouter), а НЕ свойство
        # лида. Решение владельца (2026-07-13): за наши сбои лида НЕ штрафуем и
        # НИКОГДА не пушим invalid партнёру — просто возвращаем в пул как есть
        # (count_attempt=False). PEER_FLOOD пейсится cooldown'ом, LLM
        # восстанавливается сам. Раньше был count_attempt=True → на 3-й наш сбой
        # валидный лид сгорал в invalid у партнёра (порча чужой воронки).
        if verdict == "transient":
            try:
                await self._partner_crm.reinsert_into_pool(
                    account_id=account_id, username=username, gender=gender,
                )
            except Exception as r_err:
                log.warning(
                    "auto_runner[%s]: reinsert failed for %s: %s",
                    account_id, username, r_err,
                )
            # Fix #5: если это TG anti-spam flood — триггерим SpamBot recovery
            # + cooldown. Все остальные transient'ы (например "user was
            # deleted") просто пропускают слот, следующий продолжит.
            if self._is_flood_error(err_code, err_msg):
                try:
                    await self._trigger_flood_recovery(
                        account_id, err_code, err_msg, seconds=err_seconds,
                    )
                except Exception:
                    log.exception(
                        "auto_runner[%s]: flood recovery threw, continuing",
                        account_id,
                    )
            else:
                log.info(
                    "auto_runner[%s]: transient error (%s), continuing on next slot",
                    account_id, err_code,
                )
            return

        # NEEDS_OPERATOR — наша вина. Лида возвращаем в пул (без штрафа —
        # простая модель не штрафует лида вообще), останавливаем автомат.
        try:
            await self._partner_crm.reinsert_into_pool(
                account_id=account_id, username=username, gender=gender,
            )
        except Exception as r_err:
            log.warning(
                "auto_runner[%s]: reinsert failed for %s: %s",
                account_id, username, r_err,
            )
        log.info(
            "auto_runner[%s]: self-stop (needs_operator:%s)", account_id, err_code,
        )
        return f"needs_operator:{err_code}"

    async def _do_self_stop(self, account_id: int, reason: str) -> None:
        """Самостоп loop-task'а изнутри.

        Используется когда loop сам решает выйти (session_completed или
        транзиентная ошибка CRM). Отличие от публичного `stop()` — не
        вызывает cancel на своей же task'е (мы внутри неё).
        """
        log.info(
            "auto_runner[%s]: self-stop start reason=%s", account_id, reason,
        )
        self._tasks.pop(account_id, None)

        try:
            await _set_running(
                account_id, False, last_status=f"stopped ({reason})",
            )
            await _clear_session(account_id)
        except Exception:
            log.exception(
                "auto_runner[%s]: self-stop failed to mark is_running=false",
                account_id,
            )

        status = (
            Status.SUCCESS if reason == "session_completed" else Status.ERROR
        )
        try:
            await bus.publish(
                module=Module.WORKER_AUTO_RUNNER,
                type=EventType.AUTO_RUNNER_STOPPED,
                status=status,
                account_id=account_id,
                data={"reason": reason},
            )
        except Exception:
            log.exception(
                "auto_runner[%s]: self-stop failed to publish AUTO_RUNNER_STOPPED",
                account_id,
            )

        log.info("auto_runner[%s]: self-stop done", account_id)

    async def _chunked_sleep(self, account_id: int, delay: float) -> None:
        """Спим короткими чанками, проверяя жив ли воркер. Это уменьшает
        задержку реакции на disconnect воркера без подписки на отдельный
        канал.
        """
        elapsed = 0.0
        while elapsed < delay:
            chunk = min(_SLEEP_CHUNK_SEC, delay - elapsed)
            await asyncio.sleep(chunk)
            elapsed += chunk
            if self._get_wrapper(account_id) is None:
                return

    # ─── Утилиты ─────────────────────────────────────────────────────

    @staticmethod
    def _normalize_username(raw: str) -> str:
        if not isinstance(raw, str):
            return ""
        u = raw.strip()
        if u.startswith("@"):
            u = u[1:]
        return u
