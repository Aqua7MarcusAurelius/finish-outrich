"""
Враппер Telegram для воркера.

Единственная точка общения с Telegram внутри проекта.
Любой модуль, которому нужен Telegram, ходит только через команды враппера.

Жизненный цикл:
    w = TelegramWrapper(...)
    await w.connect()               # подключился через primary или fallback
    w.on_new_message(handler)       # (опц.) подписка на входящие
    ... send_message / get_dialogs / get_history / read_message ...
    await w.disconnect()

См. docs/wrapper.md.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import socks  # из пакета pysocks
from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyError,
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession

from core import bus
from core.events import EventType, Module, Status

log = logging.getLogger(__name__)


# Ошибки Telegram, которые означают что сессия мертва — переключением
# прокси это не починить, нужна переавторизация.
_SESSION_EXPIRED_ERRORS: tuple[type[Exception], ...] = (
    AuthKeyError,
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedError,
    UserDeactivatedBanError,
)


# ─────────────────────────────────────────────────────────────────────
# Свои исключения — чтобы воркер/авторизация реагировали адресно
# ─────────────────────────────────────────────────────────────────────

class WrapperError(Exception):
    """Базовое исключение враппера."""


class SessionExpired(WrapperError):
    """Сессия протухла — требуется переавторизация."""


class ProxyUnavailable(WrapperError):
    """Оба прокси недоступны — воркер должен остановиться."""


class NotConnected(WrapperError):
    """Команда вызвана до connect()."""


# ─────────────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────────────

def _parse_socks5(url: str) -> tuple:
    """
    `socks5://user:pass@host:port` → кортеж для параметра `proxy` Telethon:
    (socks.SOCKS5, host, port, rdns=True, user, pass)
    """
    parsed = urlparse(url)
    if parsed.scheme != "socks5":
        raise ValueError(f"Поддерживается только socks5, получено: {parsed.scheme!r}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"Некорректный прокси: {url}")
    return (
        socks.SOCKS5,
        parsed.hostname,
        parsed.port,
        True,  # rdns — резолвим DNS на стороне прокси, не на стороне клиента
        parsed.username or None,
        parsed.password or None,
    )


def mask_proxy(url: str | None) -> str:
    """Замазать user:pass в прокси-строке. Для публикаций в шину."""
    if not url:
        return ""
    try:
        p = urlparse(url)
    except Exception:
        return "***"
    if p.username or p.password:
        return f"{p.scheme}://***@{p.hostname}:{p.port}"
    return url


# ─────────────────────────────────────────────────────────────────────
# Сериализация сообщения Telethon → dict (для get_history и хендлеров)
# ─────────────────────────────────────────────────────────────────────

def serialize_message(m: Any) -> dict[str, Any]:
    """
    Минимальный снимок сообщения.
    Полная распаковка медиа/пересылок — задача модуля истории (Этап 4).
    """
    fwd = getattr(m, "forward", None)
    reply = getattr(m, "reply_to", None)
    return {
        "telegram_message_id": getattr(m, "id", None),
        "date": getattr(m, "date", None),
        "is_outgoing": bool(getattr(m, "out", False)),
        "text": getattr(m, "message", None) or None,
        "reply_to_msg_id": getattr(reply, "reply_to_msg_id", None) if reply else None,
        "forward_from_user_id": getattr(fwd, "sender_id", None) if fwd else None,
        "forward_date": getattr(fwd, "date", None) if fwd else None,
        "media_group_id": getattr(m, "grouped_id", None),
        "has_media": getattr(m, "media", None) is not None,
    }


# ─────────────────────────────────────────────────────────────────────
# Враппер
# ─────────────────────────────────────────────────────────────────────

class TelegramWrapper:
    """
    Одна инстанция на один Telegram-аккаунт. Живёт внутри воркера
    или внутри процесса модуля авторизации (там без require_authorized).
    """

    def __init__(
        self,
        *,
        account_id: int | None,
        api_id: int,
        api_hash: str,
        session_data: bytes | None,
        proxy_primary: str,
        proxy_fallback: str | None = None,
        connection_retries: int = 2,
        connection_timeout: int = 20,
    ):
        if not api_id or not api_hash:
            raise ValueError("TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы в .env")
        if not proxy_primary:
            raise ValueError("proxy_primary обязателен")

        self.account_id = account_id
        self.api_id = api_id
        self.api_hash = api_hash
        self.proxy_primary = proxy_primary
        self.proxy_fallback = proxy_fallback
        self.connection_retries = connection_retries
        self.connection_timeout = connection_timeout

        # StringSession сериализуется в UTF-8 строку — храним её в bytea БД как есть
        self._initial_session: str = (
            session_data.decode("utf-8") if session_data else ""
        )
        self._client: TelegramClient | None = None
        self._active_proxy: str | None = None

    # ─── Свойства ──────────────────────────────────────────────────

    @property
    def client(self) -> TelegramClient:
        if self._client is None:
            raise NotConnected("Сначала вызвать connect()")
        return self._client

    @property
    def active_proxy(self) -> str | None:
        return self._active_proxy

    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    def get_session_data(self) -> bytes:
        """
        Текущая строка сессии в байтах — для записи в accounts.session_data.
        После успешного connect/login Telethon обновляет внутреннее состояние
        сессии, поэтому пересохранять имеет смысл периодически.
        """
        if self._client is not None:
            try:
                s = self._client.session.save()
                if s:
                    return s.encode("utf-8")
            except Exception:
                log.exception("wrapper: failed to save session account=%s", self.account_id)
        return self._initial_session.encode("utf-8")

    # ─── Подключение ───────────────────────────────────────────────

    async def connect(self, *, require_authorized: bool = True) -> None:
        """
        Поднять соединение через primary; если не вышло — через fallback.
        Если оба отвалились — публикуем system.error и кидаем ProxyUnavailable.

        require_authorized=False используется в модуле авторизации, где
        сессии ещё нет — и это нормально (будет send_code_request).
        """
        proxies: list[str] = [self.proxy_primary]
        if self.proxy_fallback:
            proxies.append(self.proxy_fallback)

        last_error: Exception | None = None

        for proxy_url in proxies:
            client: TelegramClient | None = None
            try:
                client = self._make_client(proxy_url)
                await asyncio.wait_for(client.connect(), timeout=self.connection_timeout)

                if require_authorized:
                    if not await client.is_user_authorized():
                        # Ключ есть, но Telegram его не принял — значит мёртвый.
                        await client.disconnect()
                        await self._publish_session_expired("is_user_authorized=False")
                        raise SessionExpired("Сессия не авторизована")

                self._client = client
                self._active_proxy = proxy_url
                log.info(
                    "wrapper connected account=%s proxy=%s",
                    self.account_id, mask_proxy(proxy_url),
                )
                return

            except SessionExpired:
                # Уже опубликовано, прокидываем наверх
                raise
            except _SESSION_EXPIRED_ERRORS as e:
                # Сессия мертва — нет смысла пробовать fallback
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                await self._publish_session_expired(str(e))
                raise SessionExpired(str(e)) from e
            except Exception as e:
                # Проблема сетевая/прокси — пробуем следующий
                log.warning(
                    "wrapper connect failed account=%s proxy=%s error=%s",
                    self.account_id, mask_proxy(proxy_url), e,
                )
                last_error = e
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                continue

        # Оба прокси провалились
        err_text = (
            f"оба прокси недоступны (последняя ошибка: {last_error})"
            if last_error else "оба прокси недоступны"
        )
        await bus.publish(
            module=Module.WRAPPER,
            type=EventType.SYSTEM_ERROR,
            status=Status.ERROR,
            account_id=self.account_id,
            data={
                "message": err_text,
                "proxy_primary": mask_proxy(self.proxy_primary),
                "proxy_fallback": mask_proxy(self.proxy_fallback),
            },
        )
        raise ProxyUnavailable(err_text)

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception:
            log.exception("wrapper disconnect error account=%s", self.account_id)
        finally:
            self._client = None
            self._active_proxy = None

    # ─── Команды (wrapper.md) ──────────────────────────────────────

    async def send_message(
        self,
        dialog: int | str,
        text: str,
        *,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        """Отправить текст. dialog — entity (id, @username, телефон)."""
        async def _do():
            return await self.client.send_message(
                entity=dialog, message=text, reply_to=reply_to,
            )
        msg = await self._guard(_do)
        return {
            "telegram_message_id": getattr(msg, "id", None),
            "date": getattr(msg, "date", None),
        }

    async def read_message(
        self,
        dialog: int | str,
        message: int | None = None,
    ) -> bool:
        """
        Отметить прочитанным.
        message=None → весь диалог до последнего входящего.
        """
        async def _do():
            return await self.client.send_read_acknowledge(
                entity=dialog, message=message,
            )
        return bool(await self._guard(_do))

    async def get_dialogs(self, limit: int | None = None) -> list[dict[str, Any]]:
        """
        Список диалогов. Возвращаем только личные (users) — группы/каналы
        в этой системе не поддерживаются (architecture.md).
        """
        if self._client is None:
            raise NotConnected("Сначала вызвать connect()")
        out: list[dict[str, Any]] = []
        try:
            async for d in self._client.iter_dialogs(limit=limit):
                if not getattr(d, "is_user", False):
                    continue
                entity = d.entity
                last_msg = d.message
                out.append({
                    "telegram_user_id": getattr(entity, "id", None),
                    "username": getattr(entity, "username", None),
                    "first_name": getattr(entity, "first_name", None),
                    "last_name": getattr(entity, "last_name", None),
                    "phone": getattr(entity, "phone", None),
                    "is_bot": getattr(entity, "bot", False),
                    "is_contact": getattr(entity, "contact", False),
                    "unread_count": getattr(d, "unread_count", 0),
                    "last_message_date": getattr(last_msg, "date", None) if last_msg else None,
                })
        except _SESSION_EXPIRED_ERRORS as e:
            await self._publish_session_expired(str(e))
            raise SessionExpired(str(e)) from e
        return out

    async def get_history(
        self,
        dialog: int | str,
        *,
        limit: int = 100,
        offset_id: int = 0,
    ) -> list[dict[str, Any]]:
        """Кусок истории диалога. offset_id=0 — с последнего."""
        if self._client is None:
            raise NotConnected("Сначала вызвать connect()")
        out: list[dict[str, Any]] = []
        try:
            async for m in self._client.iter_messages(
                entity=dialog, limit=limit, offset_id=offset_id,
            ):
                out.append(serialize_message(m))
        except _SESSION_EXPIRED_ERRORS as e:
            await self._publish_session_expired(str(e))
            raise SessionExpired(str(e)) from e
        return out

    # ─── Подписка на события Telegram ──────────────────────────────

    def on_new_message(
        self,
        handler: Callable[[events.NewMessage.Event], Awaitable[None]],
        *,
        incoming: bool = True,
        outgoing: bool = True,
    ) -> None:
        """
        Зарегистрировать хендлер входящих/исходящих сообщений.
        Хендлер сам решает что делать — wrapper не лезет в бизнес-логику.
        """
        if self._client is None:
            raise NotConnected("Сначала вызвать connect()")
        self._client.add_event_handler(
            handler,
            events.NewMessage(incoming=incoming, outgoing=outgoing),
        )

    # ─── Внутренности ──────────────────────────────────────────────

    def _make_client(self, proxy_url: str) -> TelegramClient:
        return TelegramClient(
            StringSession(self._initial_session),
            self.api_id,
            self.api_hash,
            proxy=_parse_socks5(proxy_url),
            connection_retries=self.connection_retries,
            timeout=self.connection_timeout,
        )

    async def _guard(self, coro_fn: Callable[[], Awaitable[Any]]) -> Any:
        """
        Обёртка вокруг Telethon-вызова: ловит протухшую сессию,
        публикует событие и поднимает SessionExpired наружу.
        """
        if self._client is None:
            raise NotConnected("Сначала вызвать connect()")
        try:
            return await coro_fn()
        except _SESSION_EXPIRED_ERRORS as e:
            await self._publish_session_expired(str(e))
            raise SessionExpired(str(e)) from e

    async def _publish_session_expired(self, reason: str) -> None:
        await bus.publish(
            module=Module.WRAPPER,
            type=EventType.ACCOUNT_SESSION_EXPIRED,
            status=Status.ERROR,
            account_id=self.account_id,
            data={"reason": reason},
        )