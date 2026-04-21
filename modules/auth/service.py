"""
Модуль авторизации. Проводит пользователя через многошаговую
авторизацию Telegram: start → code → (опц.) 2fa → success.

Состояние сессии — в Redis (auth_session:{id}, TTL 15 мин).
TelegramClient и phone_code_hash — в памяти процесса.
При рестарте процесса активные авторизации теряются (docs/auth.md).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from core import bus, db
from core import redis as redis_mod
from core.config import settings
from core.events import EventType, Module, Status
from core.proxy import check_socks5, parse_socks5

log = logging.getLogger(__name__)


PHASE_CODE_SENT = "code_sent"
PHASE_2FA_REQUIRED = "2fa_required"
PHASE_DONE = "done"
PHASE_FAILED = "failed"

SESSION_TTL_SEC = 15 * 60
REDIS_KEY_PREFIX = "auth_session:"

_PHONE_RX = re.compile(r"^\+\d{5,20}$")


# ─────────────────────────────────────────────────────────────────────
# Исключения (все маппятся в {"error": {"code", "message"}} в routes.py)
# ─────────────────────────────────────────────────────────────────────

class AuthError(Exception):
    code: str = "AUTH_ERROR"
    message: str = ""
    status_code: int = 400

    def __init__(self, message: str | None = None):
        self.message = message or self.__class__.message or self.code
        super().__init__(self.message)


class SessionNotFound(AuthError):
    code = "SESSION_NOT_FOUND"
    message = "Сессия авторизации не найдена"
    status_code = 404


class SessionExpired(AuthError):
    code = "SESSION_EXPIRED"
    message = "Сессия авторизации устарела, начните заново"
    status_code = 410


class PhoneInvalid(AuthError):
    code = "PHONE_INVALID"
    message = "Некорректный номер телефона"


class PhoneBanned(AuthError):
    code = "PHONE_BANNED"
    message = "Номер заблокирован Telegram"


class ProxyCheckFailed(AuthError):
    code = "PROXY_CHECK_FAILED"
    message = "Прокси недоступен"


class CodeInvalid(AuthError):
    code = "CODE_INVALID"
    message = "Неверный код"


class CodeExpired(AuthError):
    code = "CODE_EXPIRED"
    message = "Код истёк, начните заново"
    status_code = 410


class PasswordInvalid(AuthError):
    code = "PASSWORD_INVALID"
    message = "Неверный пароль 2FA"


class AccountNotFound(AuthError):
    code = "ACCOUNT_NOT_FOUND"
    message = "Аккаунт не найден"
    status_code = 404


class BadPhase(AuthError):
    code = "BAD_PHASE"
    message = "Операция недоступна в текущей фазе"
    status_code = 409


class ApiCredentialsInvalid(AuthError):
    code = "API_CREDENTIALS_INVALID"
    message = "TELEGRAM_API_ID / TELEGRAM_API_HASH неверны"


# ─────────────────────────────────────────────────────────────────────

@dataclass
class _Live:
    """Живые объекты сессии — только в памяти процесса."""
    client: TelegramClient
    phone_code_hash: str | None = None
    reauth_account_id: int | None = None


class AuthService:
    def __init__(self) -> None:
        self._live: dict[str, _Live] = {}

    # ── Публичные методы ──────────────────────────────────────────

    async def start(
        self,
        *,
        phone: str,
        name: str,
        proxy_primary: str,
        proxy_fallback: str,
        reauth_account_id: int | None = None,
    ) -> dict[str, Any]:
        self._validate_phone(phone)
        self._validate_api_creds()

        # Проверяем оба прокси
        primary_res = await check_socks5(proxy_primary)
        if not primary_res["ok"]:
            raise ProxyCheckFailed(
                f"Основной прокси недоступен: {primary_res.get('error', 'unknown')}"
            )
        fallback_res = await check_socks5(proxy_fallback)
        if not fallback_res["ok"]:
            raise ProxyCheckFailed(
                f"Запасной прокси недоступен: {fallback_res.get('error', 'unknown')}"
            )

        session_id = uuid.uuid4().hex

        # Поднимаем Telethon через primary
        client = TelegramClient(
            StringSession(),
            settings.TELEGRAM_API_ID,
            settings.TELEGRAM_API_HASH,
            proxy=parse_socks5(proxy_primary),
            connection_retries=2,
            timeout=20,
        )
        try:
            await asyncio.wait_for(client.connect(), timeout=20)
        except Exception as e:
            log.warning("auth.start: connect failed: %s", e)
            await _safe_disconnect(client)
            raise ProxyCheckFailed(f"Не удалось подключиться к Telegram: {e}")

        # Отправляем код
        try:
            sent_code = await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            await _safe_disconnect(client)
            raise PhoneInvalid()
        except PhoneNumberBannedError:
            await _safe_disconnect(client)
            raise PhoneBanned()
        except ApiIdInvalidError:
            await _safe_disconnect(client)
            raise ApiCredentialsInvalid()
        except FloodWaitError as e:
            await _safe_disconnect(client)
            raise AuthError(f"FloodWait: подождите {e.seconds} секунд")
        except Exception as e:
            log.exception("auth.start: send_code_request failed")
            await _safe_disconnect(client)
            raise AuthError(f"Ошибка отправки кода: {e}")

        self._live[session_id] = _Live(
            client=client,
            phone_code_hash=getattr(sent_code, "phone_code_hash", None),
            reauth_account_id=reauth_account_id,
        )

        await self._write_state(
            session_id,
            {
                "phase": PHASE_CODE_SENT,
                "phone": phone,
                "name": name,
                "proxy_primary": proxy_primary,
                "proxy_fallback": proxy_fallback,
                "reauth_account_id": reauth_account_id,
                "created_at": bus.now_utc().isoformat(),
                "error": None,
            },
        )

        return {"session_id": session_id, "status": PHASE_CODE_SENT}

    async def submit_code(self, session_id: str, code: str) -> dict[str, Any]:
        state = await self._load_state(session_id)
        if state["phase"] != PHASE_CODE_SENT:
            raise BadPhase(
                f"Ожидалась фаза {PHASE_CODE_SENT}, сейчас {state['phase']}"
            )

        live = self._live.get(session_id)
        if live is None:
            raise SessionExpired("Клиент не найден в памяти процесса")

        try:
            await live.client.sign_in(
                phone=state["phone"],
                code=code,
                phone_code_hash=live.phone_code_hash,
            )
        except PhoneCodeInvalidError:
            raise CodeInvalid()
        except PhoneCodeEmptyError:
            raise CodeInvalid("Пустой код")
        except PhoneCodeExpiredError:
            await self._mark_failed(session_id, "код истёк")
            raise CodeExpired()
        except SessionPasswordNeededError:
            await self._write_state(session_id, {**state, "phase": PHASE_2FA_REQUIRED})
            return {"status": PHASE_2FA_REQUIRED}
        except Exception as e:
            log.exception("auth.submit_code: sign_in failed")
            await self._mark_failed(session_id, str(e))
            raise AuthError(f"Ошибка авторизации: {e}")

        return await self._finalize(session_id, state)

    async def submit_password(self, session_id: str, password: str) -> dict[str, Any]:
        state = await self._load_state(session_id)
        if state["phase"] != PHASE_2FA_REQUIRED:
            raise BadPhase(
                f"Ожидалась фаза {PHASE_2FA_REQUIRED}, сейчас {state['phase']}"
            )

        live = self._live.get(session_id)
        if live is None:
            raise SessionExpired("Клиент не найден в памяти процесса")

        try:
            await live.client.sign_in(password=password)
        except PasswordHashInvalidError:
            raise PasswordInvalid()
        except Exception as e:
            log.exception("auth.submit_password: sign_in failed")
            await self._mark_failed(session_id, str(e))
            raise AuthError(f"Ошибка 2FA: {e}")

        return await self._finalize(session_id, state)

    async def get_status(self, session_id: str) -> dict[str, Any]:
        state = await self._load_state(session_id)
        return {
            "session_id": session_id,
            "phase": state["phase"],
            "phone": state.get("phone"),
            "created_at": state.get("created_at"),
            "error": state.get("error"),
        }

    async def cancel(self, session_id: str) -> None:
        live = self._live.pop(session_id, None)
        if live is not None:
            await _safe_disconnect(live.client)
        client = redis_mod.get_client()
        await client.delete(f"{REDIS_KEY_PREFIX}{session_id}")

    async def start_reauth(self, *, account_id: int) -> dict[str, Any]:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, phone, proxy_primary, proxy_fallback "
                "FROM accounts WHERE id = $1",
                account_id,
            )
        if row is None:
            raise AccountNotFound()
        return await self.start(
            phone=row["phone"],
            name=row["name"],
            proxy_primary=row["proxy_primary"],
            proxy_fallback=row["proxy_fallback"],
            reauth_account_id=account_id,
        )

    async def shutdown(self) -> None:
        """Закрыть все активные клиенты при остановке приложения."""
        for _, live in list(self._live.items()):
            await _safe_disconnect(live.client)
        self._live.clear()

    # ── Внутренности ───────────────────────────────────────────────

    def _validate_phone(self, phone: str) -> None:
        if not _PHONE_RX.match(phone):
            raise PhoneInvalid()

    def _validate_api_creds(self) -> None:
        if not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
            raise AuthError(
                "TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы в .env "
                "— получите на https://my.telegram.org"
            )

    async def _write_state(self, session_id: str, state: dict) -> None:
        client = redis_mod.get_client()
        await client.set(
            f"{REDIS_KEY_PREFIX}{session_id}",
            json.dumps(state, ensure_ascii=False, default=str).encode("utf-8"),
            ex=SESSION_TTL_SEC,
        )

    async def _load_state(self, session_id: str) -> dict:
        client = redis_mod.get_client()
        raw = await client.get(f"{REDIS_KEY_PREFIX}{session_id}")
        if raw is None:
            raise SessionNotFound()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            raise SessionExpired("Повреждённое состояние")

    async def _mark_failed(self, session_id: str, error: str) -> None:
        try:
            state = await self._load_state(session_id)
            await self._write_state(
                session_id, {**state, "phase": PHASE_FAILED, "error": error},
            )
        except Exception:
            pass

    async def _finalize(self, session_id: str, state: dict) -> dict[str, Any]:
        """Запись в БД + публикация события + cleanup. Общий финал для code/2fa."""
        live = self._live.get(session_id)
        if live is None:
            raise SessionExpired()

        session_str = live.client.session.save() or ""
        session_bytes = session_str.encode("utf-8")

        reauth_account_id = (
            state.get("reauth_account_id") or live.reauth_account_id
        )

        pool = db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if reauth_account_id is not None:
                    row = await conn.fetchrow(
                        """
                        UPDATE accounts
                           SET session_data = $1,
                               is_active    = TRUE,
                               updated_at   = NOW()
                         WHERE id = $2
                         RETURNING id
                        """,
                        session_bytes, reauth_account_id,
                    )
                    if row is None:
                        raise AccountNotFound()
                    account_id = row["id"]
                    event_type = EventType.ACCOUNT_REAUTHORIZED
                else:
                    account_id = await conn.fetchval(
                        """
                        INSERT INTO accounts
                            (name, phone, session_data, proxy_primary,
                             proxy_fallback, is_active, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, TRUE, NOW(), NOW())
                        RETURNING id
                        """,
                        state["name"], state["phone"], session_bytes,
                        state["proxy_primary"], state["proxy_fallback"],
                    )
                    event_type = EventType.ACCOUNT_CREATED

        await bus.publish(
            module=Module.AUTH,
            type=event_type,
            status=Status.SUCCESS,
            account_id=account_id,
            data={"account_id": account_id, "phone": state["phone"]},
        )

        await self._write_state(
            session_id, {**state, "phase": PHASE_DONE, "account_id": account_id},
        )

        await _safe_disconnect(live.client)
        self._live.pop(session_id, None)

        return {"status": PHASE_DONE, "account_id": account_id}


async def _safe_disconnect(client: TelegramClient) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass