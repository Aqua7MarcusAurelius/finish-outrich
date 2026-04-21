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
from typing import Any

from telethon import events as tg_events

from core import bus
from core import minio as minio_mod
from core.config import settings
from core.events import EventType, Module, Status
from modules.worker.wrapper import (
    ProxyUnavailable,  # noqa: F401 (публичный API подмодуля, наружу пробрасывается менеджером)
    SessionExpired,
    TelegramWrapper,
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
    ):
        self.account_id = account_id
        self.account_name = account_name

        self.wrapper = TelegramWrapper(
            account_id=account_id,
            api_id=settings.TELEGRAM_API_ID,
            api_hash=settings.TELEGRAM_API_HASH,
            session_data=session_data,
            proxy_primary=proxy_primary,
            proxy_fallback=proxy_fallback,
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

    # ─── Жизненный цикл ────────────────────────────────────────────

    async def run(self) -> None:
        """
        Возвращается когда:
        - вызвали stop()
        - сессия протухла (SessionExpired)
        - оба прокси отвалились (ProxyUnavailable)
        Исключения наружу — ловит WorkerManager.
        """
        # Импорт здесь — чтобы избежать циклических импортов через
        # history_sync → wrapper → history_sync.
        from modules.history_sync.service import HistorySyncService

        await self.wrapper.connect(require_authorized=True)

        # Прогрев entity-кэша Telethon: StringSession не сохраняет entity
        # (access_hash) между рестартами, без get_dialogs на старте
        # send_message(user_id) падает с "Could not find input entity".
        # Результат переиспользуем для нагона — один запрос вместо двух.
        dialogs_snapshot: list[dict] | None = None
        try:
            dialogs_snapshot = await self.wrapper.get_dialogs(limit=None)
        except SessionExpired:
            raise
        except Exception:
            log.exception(
                "worker account=%s: failed to warm entity cache",
                self.account_id,
            )

        # Подписка на новые сообщения
        self.wrapper.on_new_message(
            self._on_new_message, incoming=True, outgoing=True,
        )

        await bus.publish(
            module=Module.WORKER,
            type=EventType.WORKER_STARTED,
            status=Status.SUCCESS,
            account_id=self.account_id,
            data={"name": self.account_name},
        )

        # Нагон — параллельная задача, не блокирует основной цикл.
        # Новые сообщения ловит handler, старые догружает нагон.
        # Дубли отсекает unique-индекс (dialog_id, telegram_message_id).
        sync_service = HistorySyncService(
            account_id=self.account_id,
            wrapper=self.wrapper,
            dialogs_snapshot=dialogs_snapshot,
        )
        self._sync_task = asyncio.create_task(self._run_sync_safe(sync_service))

        try:
            # Блокируемся до stop_event. Telethon-клиент сам крутит
            # свой internal event loop и вызывает наши handlers.
            await self._stop_event.wait()
        finally:
            # Отменяем нагон если ещё не завершился
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

            # Дожидаемся inflight handler'ов (таймаут — 10 сек на всё)
            try:
                await asyncio.wait_for(self._inflight_zero.wait(), timeout=10)
            except asyncio.TimeoutError:
                log.warning(
                    "worker account=%s: inflight timeout, force shutdown",
                    self.account_id,
                )
            await self.wrapper.disconnect()

        # Если handler поймал SessionExpired — пробрасываем наружу
        if self._pending_exception is not None:
            raise self._pending_exception

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
