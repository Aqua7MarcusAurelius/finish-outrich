"""
Жизненный цикл воркера одного Telegram-аккаунта.

Отдельный asyncio-таск: поднимает TelegramWrapper, подписывается
на входящие/исходящие сообщения, публикует их в шину.

Менеджер воркеров управляет жизненным циклом — см. worker_manager/service.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon import events as tg_events

from core import bus
from core.config import settings
from core.events import EventType, Module, Status
from modules.worker.wrapper import (
    ProxyUnavailable,
    SessionExpired,
    TelegramWrapper,
    serialize_message,
)

log = logging.getLogger(__name__)


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
        # Флаг чтобы при остановке дождаться уходящего message.received
        self._inflight: int = 0
        self._inflight_zero = asyncio.Event()
        self._inflight_zero.set()

    # ─── Жизненный цикл ────────────────────────────────────────────

    async def run(self) -> None:
        """
        Основной цикл воркера. Возвращается когда:
        - вызвали stop()
        - сессия протухла (SessionExpired)
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
            # Дожидаемся inflight message.received (таймаут — 10 сек на всё)
            try:
                await asyncio.wait_for(self._inflight_zero.wait(), timeout=10)
            except asyncio.TimeoutError:
                log.warning(
                    "worker account=%s: inflight timeout, force shutdown",
                    self.account_id,
                )
            await self.wrapper.disconnect()

    async def stop(self) -> None:
        """Попросить воркер остановиться. Идемпотентно."""
        self._stop_event.set()

    # ─── Handlers ──────────────────────────────────────────────────

    async def _on_new_message(self, event: tg_events.NewMessage.Event) -> None:
        """
        Входящее или исходящее сообщение Telegram → публикация message.received.

        Распаковкой медиа/записью в БД займётся модуль истории на Этапе 4.
        Сейчас публикуем минимальный снимок — модуль истории получит его
        из шины и обработает сам.
        """
        if self._stop_event.is_set():
            return

        self._inflight += 1
        self._inflight_zero.clear()
        try:
            msg = event.message
            snapshot = serialize_message(msg)

            # Кто собеседник — нужен чтобы модуль истории нашёл dialog
            sender_id: int | None = None
            try:
                sender_id = (
                    msg.peer_id.user_id if hasattr(msg.peer_id, "user_id") else None
                )
            except Exception:
                sender_id = None

            await bus.publish(
                module=Module.WRAPPER,
                type=EventType.MESSAGE_RECEIVED,
                status=Status.SUCCESS,
                account_id=self.account_id,
                data={
                    **snapshot,
                    "telegram_user_id": sender_id,
                },
            )
        except Exception:
            log.exception(
                "worker account=%s: error in new_message handler",
                self.account_id,
            )
        finally:
            self._inflight -= 1
            if self._inflight <= 0:
                self._inflight = 0
                self._inflight_zero.set()