"""
Жизненный цикл воркера одного Telegram-аккаунта.

Отдельный asyncio-таск: поднимает TelegramWrapper, подписывается
на входящие/исходящие сообщения, качает медиа в MinIO, резолвит
профиль собеседника и публикует message.received на шину с полным
payload (snapshot + peer_profile + media[]).

Параллельно запускает нагон истории (HistorySyncService) — догрузит
всё что пропустили пока воркер не работал.

Менеджер воркеров управляет жизненным циклом — см. worker_manager/service.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from telethon import events as tg_events

from core import bus
from core import minio as minio_mod
from core.events import EventType, Module, Status
from modules.worker.wrapper import (
    ProxyUnavailable,  # noqa: F401 (публичный API подмодуля, наружу пробрасывается менеджером)
    SessionExpired,
    TelegramWrapper,
    TelethonDisconnected,  # noqa: F401 (публичный API — WorkerManager ловит и авторестартит)
    build_storage_key,
    detect_media_info,
    extract_user_profile,
    serialize_message,
)

log = logging.getLogger(__name__)

# Системный аккаунт Telegram — уведомления "You added account on Android" и т.п.
# Игнорируем целиком: не создаём диалог, не пишем сообщения, не качаем медиа.
TELEGRAM_SYSTEM_USER_ID = 777000


class Worker:
    """
    Жизненный цикл одного аккаунта.

    Использование (из WorkerManager):
        w = Worker(...)
        task = asyncio.create_task(w.run())
        ...
        await w.stop()
        await task
    """

    def __init__(
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
        on_ready: Callable[[], Awaitable[None]] | None = None,
    ):
        self.account_id = account_id
        self.account_name = account_name

        self.wrapper = TelegramWrapper(
            account_id=account_id,
            api_id=api_id,
            api_hash=api_hash,
            session_data=session_data,
            proxy_primary=proxy_primary,
            proxy_fallback=proxy_fallback,
            device_model=device_model,
            system_version=system_version,
            app_version=app_version,
            lang_code=lang_code,
            system_lang_code=system_lang_code,
            lang_pack=lang_pack,
        )

        self._stop_event = asyncio.Event()
        # При остановке ждём завершения inflight-хендлеров message.received
        self._inflight: int = 0
        self._inflight_zero = asyncio.Event()
        self._inflight_zero.set()
        # Если handler поймал SessionExpired — сохраняем сюда и роняем run()
        # её наружу, чтобы WorkerManager перевёл аккаунт в session_expired.
        self._pending_exception: Exception | None = None
        # Параллельная задача нагона — отменяется при stop()
        self._sync_task: asyncio.Task | None = None
        # Колбэк "connect прошёл, worker готов". Вызывается один раз после
        # успешного `wrapper.connect(...)` — WorkerManager подставляет свой
        # callback чтобы флипнуть Redis-статус STARTING → RUNNING только
        # после реального connect'а, а не сразу при create_task (устраняет
        # scenario-1 из аудита рассинхрона состояний).
        self._on_ready = on_ready
        # Флаг что телетон отвалился в фоне (Telethon его не эскалирует
        # в exception). Ставится когда supervisor поймал
        # wrapper.wait_until_disconnected().
        self._telethon_disconnected = False

    # ─── Жизненный цикл ────────────────────────────────────────────

    # Часть A (2026-07-13): параметры супервайзера соединения.
    # Один персистентный TelegramClient держит связь; Telethon сам реконнектит
    # сетевые блипы (auto_reconnect + connection_retries в wrapper). Супервайзер
    # вмешивается только когда прокси лёг надолго.
    RECONNECT_INTERVAL_SEC = 60   # бэкофф между попытками реконнекта (M7)
    PROBE_INTERVAL_SEC = 60       # период транспортной проверки основного прокси
    # Гистерезис (2026-07-13): столько раз пробуем ТЕКУЩИЙ прокси после обрыва,
    # прежде чем свитчнуться на fallback. Гасит churn на живых прокси, которые
    # моргнули чуть дольше 30с (Telethon уже отретраил и сдался).
    DROP_RETRIES = 2
    DROP_RETRY_INTERVAL_SEC = 15  # пауза между hysteresis-попытками текущего прокси
    OUTAGE_ALERT_EVERY = 5        # каждые N безуспешных кругов — SYSTEM_ERROR в шину

    async def run(self) -> None:
        """
        Тонкий супервайзер поверх авто-реконнекта Telethon (Часть A, 2026-07-13).

        Принцип: ОДИН персистентный TelegramClient на аккаунт живёт всё время
        работы воркера. Сетевые блипы Telethon переживает сам (auto_reconnect +
        connection_retries + retry_delay), прозрачно, на том же клиенте — без
        событий worker.restarted и без потери приёма (хендлеры и entity-кэш
        держит один клиент, догон делает catch_up=True).

        Супервайзер вмешивается ТОЛЬКО когда активный прокси лёг надолго —
        Telethon исчерпал реконнект и разрешил `client.disconnected`. Тогда:
          * последовательный свитч на fallback (полный disconnect старого →
            connect нового клиента с ТЕМ ЖЕ self._session; никогда две живые
            сессии — это фикс C3);
          * фоновый ТРАНСПОРТНЫЙ probe основного (без реальной сессии) —
            ожил → последовательный свитч обратно (без stale-состояния — H2);
          * бэкофф между попытками реконнекта (M7).

        Костыли Fix #16 (warm-up + re-subscribe) и Fix #21 (force_catch_up)
        удалены: их роль забирают персистентный клиент, авто-перевешивание
        хендлеров в connect_via и Telethon catch_up.

        Возвращает управление (или бросает) только:
          - stop() — оператор попросил остановить (штатное завершение)
          - SessionExpired — TG инвалидировал сессию, нужен reauth
        """
        # Импорт здесь — чтобы избежать циклических импортов через
        # history_sync → wrapper → history_sync.
        from modules.history_sync.service import HistorySyncService

        on_primary = True
        primary_probe_task: asyncio.Task | None = None
        primary_alive: asyncio.Event = asyncio.Event()

        try:
            # ─── 1) Начальное соединение (предпочитаем основной прокси) ───
            res = await self._connect_preferring(prefer_primary=True)
            if res is None:
                return   # stop сработал во время попыток
            on_primary = res

            # ─── 2) Хендлеры регистрируем ОДИН раз ────────────────────
            # Wrapper сам перевесит их на новый клиент при свитче прокси;
            # внутренний авто-реконнект Telethon (тот же клиент) их сохраняет.
            self.wrapper.on_new_message(
                self._on_new_message, incoming=True, outgoing=True,
            )
            self.wrapper.enable_typing_observer()

            # ─── 3) on_ready → RUNNING сразу после connect ────────────
            # Флипаем статус STARTING→RUNNING как только соединение есть,
            # не дожидаясь прогрева (get_dialogs может тянуть все диалоги).
            if self._on_ready is not None:
                try:
                    await self._on_ready()
                except Exception:
                    log.exception(
                        "worker account=%s: on_ready callback failed",
                        self.account_id,
                    )

            # ─── 4) Первый high-level запрос + снапшот диалогов ───────
            # get_me() заставляет TG слать апдейты этому клиенту; get_dialogs
            # прогревает entity-кэш и даёт снапшот для нагона (один раз).
            dialogs_snapshot: list[dict] | None = None
            try:
                await self.wrapper.get_me()
                dialogs_snapshot = await self.wrapper.get_dialogs(limit=None)
            except SessionExpired:
                raise
            except Exception:
                log.exception(
                    "worker account=%s: initial get_me/warm failed",
                    self.account_id,
                )

            # ─── 5) WORKER_STARTED + нагон (один раз) ──────────────────
            await bus.publish(
                module=Module.WORKER,
                type=EventType.WORKER_STARTED,
                status=Status.SUCCESS,
                account_id=self.account_id,
                data={"name": self.account_name, "on_primary": on_primary},
            )
            sync_service = HistorySyncService(
                account_id=self.account_id,
                wrapper=self.wrapper,
                dialogs_snapshot=dialogs_snapshot,
            )
            self._sync_task = asyncio.create_task(
                self._run_sync_safe(sync_service),
            )

            # ─── 5) Супервайзер ───────────────────────────────────────
            while not self._stop_event.is_set():
                # Пока на fallback — держим фоновый транспортный probe основного.
                if not on_primary and self.wrapper.proxy_fallback and (
                    primary_probe_task is None or primary_probe_task.done()
                ):
                    primary_alive = asyncio.Event()
                    primary_probe_task = asyncio.create_task(
                        self._probe_primary_until_alive(primary_alive),
                        name=f"worker-{self.account_id}-probe",
                    )

                stop_task = asyncio.create_task(self._stop_event.wait())
                disc_task = asyncio.create_task(
                    self.wrapper.wait_until_disconnected(),
                )
                wait_set: set[asyncio.Task] = {stop_task, disc_task}
                alive_task: asyncio.Task | None = None
                if not on_primary:
                    alive_task = asyncio.create_task(primary_alive.wait())
                    wait_set.add(alive_task)

                try:
                    done, _pending = await asyncio.wait(
                        wait_set, return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for t in wait_set:
                        if not t.done():
                            t.cancel()
                            try:
                                await t
                            except (asyncio.CancelledError, Exception):
                                pass

                if stop_task in done:
                    log.info(
                        "worker account=%s: stop requested, exiting run-loop",
                        self.account_id,
                    )
                    break

                if alive_task is not None and alive_task in done:
                    # Основной ожил → последовательный свитч обратно.
                    await self._cancel_probe(primary_probe_task)
                    primary_probe_task = None
                    log.info(
                        "worker account=%s: primary proxy restored, switching back",
                        self.account_id,
                    )
                    try:
                        await self._connect_or_switch(self.wrapper.proxy_primary)
                        on_primary = True
                        await bus.publish(
                            module=Module.WORKER,
                            type=EventType.WORKER_RESTARTED,
                            status=Status.SUCCESS,
                            account_id=self.account_id,
                            data={"reason": "primary_proxy_restored"},
                        )
                    except SessionExpired:
                        raise
                    except Exception:
                        log.warning(
                            "worker account=%s: switch back to primary failed, "
                            "re-establishing", self.account_id, exc_info=True,
                        )
                        res = await self._connect_preferring(prefer_primary=False)
                        if res is None:
                            break
                        on_primary = res
                    continue

                if disc_task in done:
                    # Активный прокси лёг (Telethon исчерпал реконнект).
                    log.info(
                        "worker account=%s: connection dropped, reconnecting",
                        self.account_id,
                    )
                    res = await self._switch_after_drop(on_primary)
                    if res is None:
                        break
                    on_primary = res
                    continue

        finally:
            # Cleanup: probe, sync, inflight, disconnect.
            await self._cancel_probe(primary_probe_task)

            if self._sync_task and not self._sync_task.done():
                self._sync_task.cancel()
                try:
                    await self._sync_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    log.exception(
                        "worker account=%s: sync task errored on cancel",
                        self.account_id,
                    )

            try:
                await asyncio.wait_for(self._inflight_zero.wait(), timeout=10)
            except asyncio.TimeoutError:
                log.warning(
                    "worker account=%s: inflight timeout, force shutdown",
                    self.account_id,
                )
            await self.wrapper.disconnect()

        # Handler поймал SessionExpired → пробрасываем наружу для менеджера.
        if self._pending_exception is not None:
            raise self._pending_exception

    async def _connect_or_switch(self, proxy_url: str) -> None:
        """Полный teardown старого клиента → новый connect на указанный прокси.

        Часть A: строго последовательно — сначала `disconnect()` (закрыть
        старое соединение полностью), потом `connect_via` (новый клиент с тем
        же self._session). Никогда две живые сессии одновременно (фикс C3).
        connect_via сам перевешивает хендлеры на новый клиент.
        """
        await self.wrapper.disconnect()
        await self.wrapper.connect_via(proxy_url, require_authorized=True)

    async def _connect_preferring(self, *, prefer_primary: bool) -> bool | None:
        """Крутит попытки connect пока не подключимся или не придёт stop.

        Пробует предпочитаемый прокси, затем другой; между полными кругами —
        бэкофф RECONNECT_INTERVAL_SEC (M7: без бэкоффа — реконнект-шторм).
        Возвращает True если в итоге на основном, False если на fallback,
        None если во время попыток сработал stop_event.
        SessionExpired пробрасывается наружу (терминально → reauth).
        """
        rounds = 0
        while not self._stop_event.is_set():
            candidates: list[tuple[str, bool]] = [(self.wrapper.proxy_primary, True)]
            if self.wrapper.proxy_fallback:
                candidates.append((self.wrapper.proxy_fallback, False))
            if not prefer_primary:
                candidates.reverse()   # пробуем fallback первым

            for proxy, is_primary in candidates:
                try:
                    await self._connect_or_switch(proxy)
                    log.info(
                        "worker account=%s: connected via %s",
                        self.account_id, "primary" if is_primary else "fallback",
                    )
                    return is_primary
                except SessionExpired:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning(
                        "worker account=%s: connect via %s failed: %s",
                        self.account_id,
                        "primary" if is_primary else "fallback", e,
                    )

            # Честный статус total-outage (2026-07-13): оба прокси недоступны.
            # Раньше цикл молча крутился вечно, а Redis-статус висел
            # RUNNING/STARTING — мониторинг врал, «прокси лёг» и «код завис»
            # выглядели одинаково. Теперь раз в OUTAGE_ALERT_EVERY кругов явно
            # сообщаем в шину (не меняя lifecycle — просто наблюдаемость).
            rounds += 1
            if rounds % self.OUTAGE_ALERT_EVERY == 0:
                try:
                    await bus.publish(
                        module=Module.WORKER,
                        type=EventType.SYSTEM_ERROR,
                        status=Status.ERROR,
                        account_id=self.account_id,
                        data={
                            "scope": "worker_connection",
                            "message": "all proxies unreachable — reconnecting",
                            "account_id": self.account_id,
                            "rounds": rounds,
                        },
                    )
                except Exception:
                    log.debug(
                        "worker account=%s: outage alert publish failed",
                        self.account_id, exc_info=True,
                    )

            if await self._sleep_or_stop(self.RECONNECT_INTERVAL_SEC):
                return None
        return None

    async def _switch_after_drop(self, on_primary: bool) -> bool | None:
        """Активный прокси лёг. Сначала ГИСТЕРЕЗИС — даём ТЕКУЩЕМУ прокси ещё
        несколько попыток, и только если он реально мёртв — последовательный
        переезд на fallback.

        Зачем: Telethon уже отретраил ~30с и разрешил `disconnected`, но живой
        серверный прокси может моргать чуть дольше (флап маршрута / обслуживание
        DC Telegram). Раньше ПЕРВЫЙ такой обрыв сразу гнал свитч на fallback +
        шторм `worker.restarted` даже на исправном прокси — отсюда мнимая
        «нестабильность хороших прокси». Если реконнект на том же прокси удался —
        НИЧЕГО не переключаем и событий не плодим.

        Возвращает новый on_primary или None (stop).
        """
        current = (
            self.wrapper.proxy_primary if on_primary else self.wrapper.proxy_fallback
        )
        for attempt in range(1, self.DROP_RETRIES + 1):
            if self._stop_event.is_set():
                return None
            try:
                await self._connect_or_switch(current)
                log.info(
                    "worker account=%s: reconnected on SAME proxy after drop "
                    "(try %d/%d) — без свитча",
                    self.account_id, attempt, self.DROP_RETRIES,
                )
                return on_primary
            except SessionExpired:
                raise
            except Exception as e:
                log.warning(
                    "worker account=%s: reconnect on current proxy failed "
                    "(try %d/%d): %s",
                    self.account_id, attempt, self.DROP_RETRIES, e,
                )
            if await self._sleep_or_stop(self.DROP_RETRY_INTERVAL_SEC):
                return None

        # Текущий прокси действительно недоступен → последовательный переезд.
        if on_primary and self.wrapper.proxy_fallback:
            try:
                await self._connect_or_switch(self.wrapper.proxy_fallback)
                await bus.publish(
                    module=Module.WORKER,
                    type=EventType.WORKER_RESTARTED,
                    status=Status.ERROR,
                    account_id=self.account_id,
                    data={"reason": "switched_to_fallback"},
                )
                return False
            except SessionExpired:
                raise
            except Exception as e:
                log.warning(
                    "worker account=%s: switch to fallback failed: %s",
                    self.account_id, e,
                )
                # fallback тоже недоступен → общий реконнект ниже (с бэкоффом).

        res = await self._connect_preferring(prefer_primary=True)
        if res is None:
            return None
        await bus.publish(
            module=Module.WORKER,
            type=EventType.WORKER_RESTARTED,
            status=Status.SUCCESS,
            account_id=self.account_id,
            data={"reason": "reconnected", "on_primary": res},
        )
        return res

    async def _probe_primary_until_alive(self, alive_event: asyncio.Event) -> None:
        """Пока на fallback — раз в PROBE_INTERVAL_SEC ТРАНСПОРТНО проверяем
        основной прокси (без реальной сессии — фикс C3). Ожил → ставим
        alive_event; сам свитч делает супервайзер (единый владелец
        соединения — без гонок и stale-состояния, фикс H2). На каждый эпизод
        fallback — свежий event.
        """
        while not self._stop_event.is_set():
            if await self._sleep_or_stop(self.PROBE_INTERVAL_SEC):
                return
            try:
                ok = await self.wrapper.probe_proxy(self.wrapper.proxy_primary)
            except asyncio.CancelledError:
                raise
            except Exception:
                ok = False
            if ok:
                log.info(
                    "worker account=%s: primary probe OK, signaling restore",
                    self.account_id,
                )
                alive_event.set()
                return

    async def _cancel_probe(self, task: asyncio.Task | None) -> None:
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _sleep_or_stop(self, seconds: float) -> bool:
        """Спим `seconds`, просыпаясь раньше если выставлен stop_event.
        Возвращает True если проснулись по stop, False если по таймауту.
        """
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run_sync_safe(self, sync_service) -> None:
        """
        Обёртка для нагона: ловит SessionExpired и сигналит воркеру
        остановку — так же как это делают входящие хендлеры.
        """
        try:
            await sync_service.run()
        except asyncio.CancelledError:
            raise
        except SessionExpired as e:
            self._pending_exception = e
            self._stop_event.set()
        except Exception:
            # Все остальные ошибки нагона НЕ валят воркер — нагон
            # уже опубликовал system.error сам. Просто завершаемся.
            log.exception(
                "worker account=%s: sync task failed (non-fatal)",
                self.account_id,
            )

    async def stop(self) -> None:
        """Попросить воркер остановиться. Идемпотентно."""
        self._stop_event.set()

    # ─── Handlers ──────────────────────────────────────────────────

    async def _on_new_message(self, event: tg_events.NewMessage.Event) -> None:
        if self._stop_event.is_set():
            return

        # Только личные чаты — группы/каналы мимо
        if not getattr(event, "is_private", False):
            return

        msg = event.message
        peer = getattr(msg, "peer_id", None)
        telegram_user_id = getattr(peer, "user_id", None) if peer else None
        if telegram_user_id is None:
            return
        if telegram_user_id == TELEGRAM_SYSTEM_USER_ID:
            return

        self._inflight += 1
        self._inflight_zero.clear()
        try:
            await self._handle_message(event, telegram_user_id)
        except SessionExpired as e:
            self._pending_exception = e
            self._stop_event.set()
        except Exception:
            log.exception(
                "worker account=%s msg=%s: handler error",
                self.account_id, getattr(msg, "id", None),
            )
        finally:
            self._inflight -= 1
            if self._inflight <= 0:
                self._inflight = 0
                self._inflight_zero.set()

    # ─── Основная логика ───────────────────────────────────────────

    async def _handle_message(
        self,
        event: tg_events.NewMessage.Event,
        telegram_user_id: int,
    ) -> None:
        # 1. Профиль собеседника — в ~всех случаях чтение из кэша Telethon
        peer_entity = await self.wrapper.resolve_event_peer(event)
        peer_profile = extract_user_profile(peer_entity)

        # 2. Базовый snapshot сообщения
        msg = event.message
        snapshot = serialize_message(msg)
        telegram_message_id = snapshot["telegram_message_id"]

        # 3. Разбор и загрузка медиа
        media_info = detect_media_info(msg)
        media_payload: list[dict[str, Any]] = []

        if media_info is not None and telegram_message_id is not None:
            storage_key = build_storage_key(
                account_id=self.account_id,
                telegram_user_id=telegram_user_id,
                telegram_message_id=telegram_message_id,
                ext=media_info["ext"],
            )
            try:
                blob = await self.wrapper.download_media_bytes(msg)
                if blob:
                    await minio_mod.put_object(
                        storage_key,
                        blob,
                        content_type=media_info.get("mime_type"),
                    )
                    entry = {k: v for k, v in media_info.items() if k != "ext"}
                    entry["storage_key"] = storage_key
                    media_payload.append(entry)
                else:
                    log.warning(
                        "worker account=%s msg=%s: empty blob from download_media_bytes",
                        self.account_id, telegram_message_id,
                    )
            except SessionExpired:
                raise
            except Exception as e:
                log.exception(
                    "worker account=%s msg=%s: media store failed",
                    self.account_id, telegram_message_id,
                )
                await bus.publish(
                    module=Module.WRAPPER,
                    type=EventType.SYSTEM_ERROR,
                    status=Status.ERROR,
                    account_id=self.account_id,
                    data={
                        "message": f"не удалось сохранить медиа: {e}",
                        "telegram_message_id": telegram_message_id,
                        "telegram_user_id": telegram_user_id,
                        "media_type": media_info.get("type"),
                    },
                )

        # 4. Публикуем событие
        await bus.publish(
            module=Module.WRAPPER,
            type=EventType.MESSAGE_RECEIVED,
            status=Status.SUCCESS,
            account_id=self.account_id,
            data={
                **snapshot,
                "telegram_user_id": telegram_user_id,
                "peer_profile": peer_profile,
                "media": media_payload,
            },
        )
