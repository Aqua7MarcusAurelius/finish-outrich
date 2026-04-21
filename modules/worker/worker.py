"""
Жизненный цикл воркера одного Telegram-аккаунта.

Отдельный asyncio-таск: поднимает TelegramWrapper, подписывается
на входящие/исходящие сообщения, качает медиа в MinIO, резолвит
профиль собеседника и публикует message.received на шину с полным
payload (snapshot + peer_profile + media[]).

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
        # Без этого протухшая сессия, обнаруженная при download_media или
        # resolve_event_peer, молча гасла бы внутри callback'а.
        self._pending_exception: Exception | None = None

    # ─── Жизненный цикл ────────────────────────────────────────────

    async def run(self) -> None:
        """
        Основной цикл воркера. Возвращается когда:
        - вызвали stop()
        - сессия протухла (SessionExpired из handler'а или connect)
        - оба прокси отвалились (ProxyUnavailable)
        Исключения наружу — ловит WorkerManager.
        """
        await self.wrapper.connect(require_authorized=True)

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

        try:
            # Блокируемся до stop_event. Telethon-клиент сам крутит
            # свой internal event loop и вызывает наши handlers.
            await self._stop_event.wait()
        finally:
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

    async def stop(self) -> None:
        """Попросить воркер остановиться. Идемпотентно."""
        self._stop_event.set()

    # ─── Handlers ──────────────────────────────────────────────────

    async def _on_new_message(self, event: tg_events.NewMessage.Event) -> None:
        """
        Обработка входящего/исходящего сообщения Telegram.

        1. Фильтруем: только private-чаты, без системного 777000
        2. Резолвим профиль собеседника (обычно из кэша Telethon)
        3. Разбираем snapshot + media_info
        4. Если есть медиа — качаем в MinIO
        5. Публикуем message.received с полным payload
        """
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
            # Сохраняем и просим остановку — run() выбросит её наружу
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
        # 1. Профиль собеседника — в ~всех случаях это чтение из кэша Telethon,
        # сетевой запрос только при "первой встрече" нового человека.
        peer_entity = await self.wrapper.resolve_event_peer(event)
        peer_profile = extract_user_profile(peer_entity)

        # 2. Базовый snapshot сообщения
        msg = event.message
        snapshot = serialize_message(msg)
        telegram_message_id = snapshot["telegram_message_id"]

        # 3. Разбор и загрузка медиа (если есть)
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
                    # в payload кладём всё кроме служебного `ext`, плюс storage_key
                    entry = {k: v for k, v in media_info.items() if k != "ext"}
                    entry["storage_key"] = storage_key
                    media_payload.append(entry)
                else:
                    # Не критично — сообщение всё равно запишется, но без файла
                    log.warning(
                        "worker account=%s msg=%s: empty blob from download_media_bytes",
                        self.account_id, telegram_message_id,
                    )
            except SessionExpired:
                # Пробрасываем наверх — остановит воркер
                raise
            except Exception as e:
                # Инфра: MinIO недоступен, качалка ошиблась — публикуем
                # system.error, но само сообщение всё равно публикуем (без media).
                # Иначе потеряем факт получения сообщения, а это хуже чем
                # потерять файл (метаданные и текст останутся).
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
