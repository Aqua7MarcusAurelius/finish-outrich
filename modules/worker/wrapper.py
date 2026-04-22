"""
Враппер Telegram для воркера.

Единственная точка общения с Telegram внутри проекта.
Любой модуль, которому нужен Telegram, ходит только через команды враппера.

Жизненный цикл:
    w = TelegramWrapper(...)
    await w.connect()                    # подключился через primary или fallback
    w.on_new_message(handler)            # (опц.) подписка на входящие
    ... send_message / get_dialogs / get_history / download_media_bytes ...
    await w.disconnect()

См. docs/wrapper.md.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyError,
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeImageSize,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
    SendMessageCancelAction,
    SendMessageTypingAction,
    User,
)

from core import bus
from core.events import EventType, Module, Status
from core.proxy import mask as mask_proxy, parse_socks5

log = logging.getLogger(__name__)


_SESSION_EXPIRED_ERRORS: tuple[type[Exception], ...] = (
    AuthKeyError,
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedError,
    UserDeactivatedBanError,
)


# ─────────────────────────────────────────────────────────────────────
# Свои исключения
# ─────────────────────────────────────────────────────────────────────

class WrapperError(Exception):
    """Базовое исключение враппера."""


class SessionExpired(WrapperError):
    """Сессия протухла — требуется переавторизация."""


class ProxyUnavailable(WrapperError):
    """Оба прокси недоступны — воркер должен остановиться."""


class NotConnected(WrapperError):
    """Команда вызвана до connect()."""


class UsernameNotFound(WrapperError):
    """@username не существует / не занят."""


class UsernameUnavailable(WrapperError):
    """@username не резолвится (privacy, это не user-тип, и т.п.)."""


# ─────────────────────────────────────────────────────────────────────
# Разбор сообщений Telethon → JSON-совместимый dict
# ─────────────────────────────────────────────────────────────────────

# Расширение файла по типу медиа на случай если Telethon не смог вычислить его сам
_DEFAULT_EXT: dict[str, str] = {
    "photo": "jpg",
    "sticker": "webp",
    "video_note": "mp4",
    "voice": "ogg",
    "gif": "mp4",
    "video": "mp4",
    "audio": "mp3",
    "document": "bin",
}


def _extract_forward_info(m: Any) -> dict[str, Any] | None:
    """
    Снимок данных о пересылке. None если сообщение не пересланное.
    Структура соответствует docs/event_bus.md → forward_from.
    """
    fwd = getattr(m, "fwd_from", None)
    if fwd is None:
        return None
    from_id = getattr(fwd, "from_id", None)
    user_id = getattr(from_id, "user_id", None) if from_id else None
    chat_id: int | None = None
    if from_id is not None:
        chat_id = (
            getattr(from_id, "channel_id", None)
            or getattr(from_id, "chat_id", None)
        )
    return {
        "user_id": user_id,
        "chat_id": chat_id,
        "name": getattr(fwd, "from_name", None),
        "date": getattr(fwd, "date", None),
    }


def serialize_message(m: Any) -> dict[str, Any]:
    """
    Базовый снимок сообщения без медиа.
    Медиа разбирает detect_media_info — отдельно, чтобы не таскать
    большие вложенные структуры когда они не нужны.
    """
    reply = getattr(m, "reply_to", None)
    fwd_info = _extract_forward_info(m)
    return {
        "telegram_message_id": getattr(m, "id", None),
        "date": getattr(m, "date", None),
        "is_outgoing": bool(getattr(m, "out", False)),
        "text": getattr(m, "message", None) or None,
        "reply_to_telegram_message_id": (
            getattr(reply, "reply_to_msg_id", None) if reply else None
        ),
        "forward_from": fwd_info,
        "media_group_id": getattr(m, "grouped_id", None),
    }


def extract_user_profile(entity: Any) -> dict[str, Any] | None:
    """
    Снимок профиля собеседника (Telethon User entity → dict).

    Достаёт всё что доступно без дополнительного GetFullUserRequest:
    username, имя, фамилия, телефон, флаги бот/контакт. Поля FullUser
    (bio, birthday, имена из адресной книги) тянутся отдельно когда
    реально нужны — например при открытии карточки диалога.

    None — если entity пустой или не User-подобный (нет id).
    """
    if entity is None:
        return None
    user_id = getattr(entity, "id", None)
    if user_id is None:
        return None
    return {
        "telegram_user_id": user_id,
        "username": getattr(entity, "username", None),
        "first_name": getattr(entity, "first_name", None),
        "last_name": getattr(entity, "last_name", None),
        "phone": getattr(entity, "phone", None),
        "is_bot": bool(getattr(entity, "bot", False)),
        "is_contact": bool(getattr(entity, "contact", False)),
    }


def detect_media_info(m: Any) -> dict[str, Any] | None:
    """
    Распаковка атрибутов медиа сообщения.

    Возвращает dict со всеми полями таблицы `media` кроме
    storage_key/transcription/description (они появляются позже —
    storage_key после загрузки в MinIO, тексты после обработки модулями).

    Служебное поле `ext` — расширение для построения storage_key, в БД не пишется.

    None — если у сообщения нет файлового медиа (включая web_page, geo, contact).
    """
    media = getattr(m, "media", None)
    if media is None:
        return None

    # ── Фото ─────────────────────────────────────────────────────────
    if isinstance(media, MessageMediaPhoto):
        file = getattr(m, "file", None)
        photo = getattr(media, "photo", None)
        ext = (getattr(file, "ext", "") or "").lstrip(".").lower() or "jpg"
        return {
            "type": "photo",
            "file_name": None,
            "telegram_file_id": (
                str(photo.id) if photo and getattr(photo, "id", None) else None
            ),
            "mime_type": getattr(file, "mime_type", None) or "image/jpeg",
            "file_size": getattr(file, "size", None),
            "duration": None,
            "width": getattr(file, "width", None),
            "height": getattr(file, "height", None),
            "ext": ext,
        }

    # ── Документ — подвид определяем по атрибутам ───────────────────
    # voice / video / video_note / audio / sticker / gif / document
    if isinstance(media, MessageMediaDocument):
        doc = getattr(media, "document", None)
        if doc is None:
            return None
        attrs = getattr(doc, "attributes", []) or []

        def _find(cls: type) -> Any | None:
            for a in attrs:
                if isinstance(a, cls):
                    return a
            return None

        sticker_attr = _find(DocumentAttributeSticker)
        animated_attr = _find(DocumentAttributeAnimated)
        video_attr = _find(DocumentAttributeVideo)
        audio_attr = _find(DocumentAttributeAudio)
        filename_attr = _find(DocumentAttributeFilename)
        imgsize_attr = _find(DocumentAttributeImageSize)

        # Порядок проверок важен: кружок — это тоже video_attr,
        # голосовое — тоже audio_attr. Сначала различаем подтипы.
        if sticker_attr:
            media_type = "sticker"
        elif video_attr and getattr(video_attr, "round_message", False):
            media_type = "video_note"
        elif audio_attr and getattr(audio_attr, "voice", False):
            media_type = "voice"
        elif animated_attr:
            media_type = "gif"
        elif video_attr:
            media_type = "video"
        elif audio_attr:
            media_type = "audio"
        else:
            media_type = "document"

        file = getattr(m, "file", None)
        ext = (getattr(file, "ext", "") or "").lstrip(".").lower()
        if not ext:
            ext = _DEFAULT_EXT.get(media_type, "bin")

        raw_duration = (
            getattr(video_attr, "duration", None) if video_attr
            else getattr(audio_attr, "duration", None) if audio_attr
            else None
        )
        duration: int | None
        if raw_duration is None:
            duration = None
        else:
            try:
                duration = int(raw_duration)
            except Exception:
                duration = None

        width = (
            getattr(video_attr, "w", None)
            or getattr(imgsize_attr, "w", None)
        )
        height = (
            getattr(video_attr, "h", None)
            or getattr(imgsize_attr, "h", None)
        )

        return {
            "type": media_type,
            "file_name": (
                getattr(filename_attr, "file_name", None) if filename_attr else None
            ),
            "telegram_file_id": (
                str(doc.id) if getattr(doc, "id", None) else None
            ),
            "mime_type": getattr(doc, "mime_type", None),
            "file_size": getattr(doc, "size", None),
            "duration": duration,
            "width": width,
            "height": height,
            "ext": ext,
        }

    # MessageMediaWebPage / Geo / Contact / Poll / Invoice / … — игнорируем
    return None


def build_storage_key(
    *,
    account_id: int,
    telegram_user_id: int,
    telegram_message_id: int,
    ext: str,
) -> str:
    """
    Ключ файла в MinIO.

    Формат: account_{account_id}/{telegram_user_id}/{telegram_message_id}.{ext}

    Используем telegram_user_id вместо внутреннего dialog_id из БД: враппер
    работает до модуля истории и не знает внутренних id. Для пары
    (account_id, telegram_user_id) у нас гарантированный уникальный
    dialog_id, так что ключ однозначен.
    """
    ext = (ext or "bin").lstrip(".").lower() or "bin"
    return f"account_{account_id}/{telegram_user_id}/{telegram_message_id}.{ext}"


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
        """Текущая строка сессии в байтах — для записи в accounts.session_data."""
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
                raise
            except _SESSION_EXPIRED_ERRORS as e:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                await self._publish_session_expired(str(e))
                raise SessionExpired(str(e)) from e
            except Exception as e:
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

    # ─── Команды ───────────────────────────────────────────────────

    async def send_message(
        self,
        dialog: int | str,
        text: str,
        *,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
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
        async def _do():
            return await self.client.send_read_acknowledge(
                entity=dialog, message=message,
            )
        return bool(await self._guard(_do))

    async def resolve_username(self, username: str) -> dict[str, Any]:
        """
        Резолв @username → снимок профиля User entity.

        Используется модулем AutoChat при старте автосессии. Telethon
        внутри делает ResolveUsernameRequest + кэширует entity, так что
        дальше send_message(user_id) работает без лишних запросов.

        Бросает:
          - UsernameNotFound — такого username нет.
          - UsernameUnavailable — резолвится, но не в User (канал/бот-клиент и т.п.).
          - SessionExpired — сессия протухла.
        """
        cleaned = (username or "").strip().lstrip("@")
        if not cleaned:
            raise UsernameUnavailable("пустой username")

        async def _do():
            return await self.client.get_entity(cleaned)

        try:
            entity = await self._guard(_do)
        except SessionExpired:
            raise
        except (UsernameNotOccupiedError, UsernameInvalidError) as e:
            raise UsernameNotFound(str(e)) from e
        except ValueError as e:
            # get_entity кидает ValueError при "Cannot find any entity…"
            raise UsernameNotFound(str(e)) from e
        except Exception as e:
            raise UsernameUnavailable(f"resolve failed: {e}") from e

        if not isinstance(entity, User):
            raise UsernameUnavailable(
                f"@{cleaned} — не пользовательский аккаунт"
            )

        profile = extract_user_profile(entity)
        if profile is None or profile.get("telegram_user_id") is None:
            raise UsernameUnavailable(f"@{cleaned} — не удалось снять профиль")
        return profile

    async def set_typing(self, user_id: int) -> None:
        """
        Включить индикатор "печатает" в private-диалоге с user_id.

        Telegram автоматически гасит индикатор ~через 6 сек, если не
        продлевать. Для длинной печати — дёргать повторно каждые ~4 сек
        (за это отвечает caller, модуль AutoChat).
        """
        async def _do():
            return await self.client(SetTypingRequest(
                peer=user_id,
                action=SendMessageTypingAction(),
            ))
        await self._guard(_do)

    async def cancel_typing(self, user_id: int) -> None:
        """
        Снять индикатор "печатает" немедленно.

        Нужно перед send_message, чтобы Telegram не показывал "печатает"
        во время реальной отправки (визуальный артефакт на 0.5 сек).
        """
        async def _do():
            return await self.client(SetTypingRequest(
                peer=user_id,
                action=SendMessageCancelAction(),
            ))
        try:
            await self._guard(_do)
        except SessionExpired:
            raise
        except Exception:
            # Отмена typing — best-effort. Сбой не валит отправку.
            log.debug(
                "wrapper account=%s: cancel_typing failed (non-fatal)",
                self.account_id,
            )

    async def get_dialogs(self, limit: int | None = None) -> list[dict[str, Any]]:
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
    ) -> list[Any]:
        """
        История диалога — сырые Telethon Message-объекты.

        Возвращаем именно объекты (а не snapshot-ы), чтобы вызывающий
        модуль (нагон) мог скачивать медиа через download_media_bytes(msg)
        без повторного запроса в Telegram. Для превращения в payload —
        вызывать serialize_message + detect_media_info поштучно.
        """
        if self._client is None:
            raise NotConnected("Сначала вызвать connect()")
        out: list[Any] = []
        try:
            async for m in self._client.iter_messages(
                entity=dialog, limit=limit, offset_id=offset_id,
            ):
                out.append(m)
        except _SESSION_EXPIRED_ERRORS as e:
            await self._publish_session_expired(str(e))
            raise SessionExpired(str(e)) from e
        return out

    async def download_media_bytes(self, msg: Any) -> bytes | None:
        """
        Скачать медиа сообщения в память. None если у сообщения нет файла.
        Ошибки сессии — через _guard → SessionExpired.
        """
        if self._client is None:
            raise NotConnected("Сначала вызвать connect()")
        if not getattr(msg, "media", None):
            return None

        async def _do():
            return await self._client.download_media(msg, file=bytes)

        result = await self._guard(_do)
        if result is None:
            return None
        if isinstance(result, (bytes, bytearray)):
            return bytes(result)
        # На всякий случай — если Telethon вдруг вернул не bytes
        return None

    async def resolve_event_peer(self, event: Any) -> Any | None:
        """
        Entity собеседника для NewMessage event (в private chat — User).

        Обычно не ходит в сеть — Telethon кэширует entity прямо в сессии.
        Запрос случается только если собеседник "первой встречи" и его
        ещё нет в кэше.

        SessionExpired пробрасывается наверх (её обязан обработать воркер),
        прочие ошибки глушим в None — профиль не критичен, сообщение
        запишется в историю и без него.
        """
        if self._client is None:
            raise NotConnected("Сначала вызвать connect()")

        async def _do():
            return await event.get_chat()

        try:
            return await self._guard(_do)
        except SessionExpired:
            raise
        except Exception:
            log.exception(
                "wrapper account=%s: resolve_event_peer failed",
                self.account_id,
            )
            return None

    # ─── Подписка на события Telegram ──────────────────────────────

    def on_new_message(
        self,
        handler: Callable[[events.NewMessage.Event], Awaitable[None]],
        *,
        incoming: bool = True,
        outgoing: bool = True,
    ) -> None:
        if self._client is None:
            raise NotConnected("Сначала вызвать connect()")
        self._client.add_event_handler(
            handler,
            events.NewMessage(incoming=incoming, outgoing=outgoing),
        )

    def enable_typing_observer(self) -> None:
        """
        Активировать публикацию dialog.typing_observed на шину при
        тайпинге собеседника в любом private-диалоге этого воркера.

        Слушает модуль AutoChat для перезапуска reply-таймера.
        Вызывается воркером один раз после connect().
        """
        if self._client is None:
            raise NotConnected("Сначала вызвать connect()")
        self._client.add_event_handler(
            self._on_user_update,
            events.UserUpdate(),
        )

    async def _on_user_update(self, event: Any) -> None:
        """
        Handler events.UserUpdate — публикует dialog.typing_observed
        только для случая "собеседник печатает".

        Другие типы user-update (uploading/recording/playing/…) игнорируем:
        модуль AutoChat реагирует только на "печатает".
        """
        try:
            # events.UserUpdate предоставляет .typing — True если
            # action=SendMessageTypingAction. Отфильтровываем всё остальное.
            if not getattr(event, "typing", False):
                return
            user_id = getattr(event, "user_id", None)
            if user_id is None:
                return
            await bus.publish(
                module=Module.WRAPPER,
                type=EventType.DIALOG_TYPING_OBSERVED,
                status=Status.SUCCESS,
                account_id=self.account_id,
                data={
                    "telegram_user_id": int(user_id),
                    "at": bus.now_utc().isoformat(),
                },
            )
        except Exception:
            # Тайпинг-события публикуются часто и не критичны — глушим,
            # чтобы handler не вывалил Telethon dispatch loop.
            log.debug(
                "wrapper account=%s: typing observer failed",
                self.account_id,
            )

    # ─── Внутренности ──────────────────────────────────────────────

    def _make_client(self, proxy_url: str) -> TelegramClient:
        return TelegramClient(
            StringSession(self._initial_session),
            self.api_id,
            self.api_hash,
            proxy=parse_socks5(proxy_url),
            connection_retries=self.connection_retries,
            timeout=self.connection_timeout,
        )

    async def _guard(self, coro_fn: Callable[[], Awaitable[Any]]) -> Any:
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
