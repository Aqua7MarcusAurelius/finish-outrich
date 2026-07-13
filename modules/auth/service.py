"""
Модуль авторизации. Проводит пользователя через многошаговую
авторизацию Telegram: start → code → (опц.) 2fa → success.

С миграции 0013 каждый аккаунт несёт собственные api_id/api_hash и
device-info; оператор задаёт их при вызове /auth/start (вместе с
телефоном, прокси и опциональным 2FA-паролем — последний backend
сам применит после кода, если Telegram запросит 2FA). Это закрепляет
уникальный fingerprint аккаунта — см. §9 PROJECT_SPEC.md.

Состояние сессии — в Redis (auth_session:{id}, TTL 15 мин).
TelegramClient, phone_code_hash и (опц.) 2FA-пароль — в памяти процесса.
При рестарте процесса активные авторизации теряются.
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
    message = "api_id / api_hash аккаунта неверны"


class FingerprintInvalid(AuthError):
    code = "FINGERPRINT_INVALID"
    message = "device_model / system_version / app_version обязательны"


# ─────────────────────────────────────────────────────────────────────

@dataclass
class _Live:
    """Живые объекты сессии — только в памяти процесса."""
    client: TelegramClient
    phone_code_hash: str | None = None
    reauth_account_id: int | None = None
    # 2FA-пароль, переданный оператором заранее в /auth/start. Если на коде
    # Telegram запросит 2FA — backend сам подставит пароль, без отдельного
    # шага /auth/2fa. Сериализовать нельзя — живёт только в памяти процесса.
    password: str | None = None
    # L4 (2026-07-13): api_hash — секрет приложения TG, держим ТОЛЬКО в памяти
    # (как password), не пишем в Redis auth_session. Сессия и так memory-bound
    # (client в _Live), при рестарте процесса теряется целиком — так что
    # хранить секрет в Redis не давало плюсов, только риск утечки.
    api_hash: str | None = None


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
        api_id: int,
        api_hash: str,
        device_model: str,
        system_version: str,
        app_version: str,
        lang_code: str = "en",
        system_lang_code: str = "en",
        lang_pack: str = "",
        password: str | None = None,
        reauth_account_id: int | None = None,
    ) -> dict[str, Any]:
        self._validate_phone(phone)
        self._validate_api_creds(api_id, api_hash)
        self._validate_fingerprint(device_model, system_version, app_version)
        lang_code = (lang_code or "en").strip() or "en"
        system_lang_code = (system_lang_code or "en").strip() or "en"
        lang_pack = (lang_pack or "").strip()

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

        # Поднимаем Telethon через primary. Поля fingerprint'а подставляются
        # явно — TelegramClient запомнит их и при последующих реконнектах
        # будет представляться Telegram'у так же. `lang_pack` сохраняется
        # в БД для документации, но в `TelegramClient.__init__` его нет
        # (внутреннее MTProto-поле, не публичный аргумент конструктора).
        client = TelegramClient(
            StringSession(),
            api_id,
            api_hash,
            proxy=parse_socks5(proxy_primary),
            connection_retries=2,
            timeout=20,
            device_model=device_model,
            system_version=system_version,
            app_version=app_version,
            lang_code=lang_code,
            system_lang_code=system_lang_code,
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
            password=password or None,
            api_hash=api_hash,   # L4: только в памяти, не в Redis
        )

        await self._write_state(
            session_id,
            {
                "phase": PHASE_CODE_SENT,
                "phone": phone,
                "name": name,
                "proxy_primary": proxy_primary,
                "proxy_fallback": proxy_fallback,
                "api_id": api_id,
                # L4: api_hash в Redis НЕ пишем — только в памяти (_Live.api_hash).
                "device_model": device_model,
                "system_version": system_version,
                "app_version": app_version,
                "lang_code": lang_code,
                "system_lang_code": system_lang_code,
                "lang_pack": lang_pack,
                # Пароль в Redis не пишем — он живёт только в памяти процесса
                # (см. _Live.password). Это компромисс: при рестарте процесса
                # сессия теряется (как было и до фичи), пароль не утекает в
                # хранилище.
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
            # Если оператор задал пароль заранее в /auth/start — применяем
            # его сразу, без отдельного шага /auth/2fa (это поведение
            # «one-shot» из §9 SPEC). Иначе — переводим в фазу 2fa_required
            # и ждём явный /auth/2fa.
            if live.password:
                return await self._apply_password_inline(session_id, state, live)
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
        """Переавторизация: используем сохранённые на аккаунте fingerprint-поля
        как есть — Telegram должен видеть «тот же клиент». Меняется только
        session_data (новый StringSession после успешного code/2fa)."""
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, phone, proxy_primary, proxy_fallback, "
                "api_id, api_hash, device_model, system_version, app_version, "
                "lang_code, system_lang_code, lang_pack "
                "FROM accounts WHERE id = $1",
                account_id,
            )
        if row is None:
            raise AccountNotFound()
        if not row["api_id"] or not row["api_hash"]:
            raise ApiCredentialsInvalid(
                "У аккаунта не задан api_id/api_hash — это аккаунт до миграции 0013, "
                "пересоздай его через мастер добавления (а не переавторизуй)"
            )
        return await self.start(
            phone=row["phone"],
            name=row["name"],
            proxy_primary=row["proxy_primary"],
            proxy_fallback=row["proxy_fallback"],
            api_id=int(row["api_id"]),
            api_hash=row["api_hash"],
            device_model=row["device_model"] or "",
            system_version=row["system_version"] or "",
            app_version=row["app_version"] or "",
            lang_code=row["lang_code"] or "en",
            system_lang_code=row["system_lang_code"] or "en",
            lang_pack=row["lang_pack"] or "",
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

    def _validate_api_creds(self, api_id: int, api_hash: str) -> None:
        if not api_id or not api_hash:
            raise ApiCredentialsInvalid()

    def _validate_fingerprint(
        self, device_model: str, system_version: str, app_version: str,
    ) -> None:
        if not device_model or not system_version or not app_version:
            raise FingerprintInvalid()

    async def _apply_password_inline(
        self, session_id: str, state: dict, live: _Live,
    ) -> dict[str, Any]:
        """SessionPasswordNeededError + у нас уже есть пароль из /auth/start —
        дотюкиваем 2FA молча, оператору отдаём готовый аккаунт (без
        промежуточной фазы 2fa_required)."""
        try:
            await live.client.sign_in(password=live.password or "")
        except PasswordHashInvalidError:
            await self._write_state(session_id, {**state, "phase": PHASE_2FA_REQUIRED})
            raise PasswordInvalid(
                "Пароль 2FA из /auth/start не подошёл — отправь правильный через /auth/2fa"
            )
        except Exception as e:
            log.exception("auth: inline password sign_in failed")
            await self._mark_failed(session_id, str(e))
            raise AuthError(f"Ошибка 2FA: {e}")
        return await self._finalize(session_id, state)

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
                    # Переавторизация: fingerprint-поля уже на аккаунте,
                    # перезаписывать их не нужно (Telegram должен видеть
                    # один и тот же клиент). Только session_data + is_active.
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
                             proxy_fallback, is_active,
                             api_id, api_hash, device_model, system_version,
                             app_version, lang_code, system_lang_code, lang_pack,
                             created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, TRUE,
                                $6, $7, $8, $9, $10, $11, $12, $13,
                                NOW(), NOW())
                        RETURNING id
                        """,
                        state["name"], state["phone"], session_bytes,
                        state["proxy_primary"], state["proxy_fallback"],
                        # L4: api_hash из памяти (_Live), а не из Redis state.
                        int(state["api_id"]), live.api_hash,
                        state["device_model"], state["system_version"],
                        state["app_version"], state.get("lang_code") or "en",
                        state.get("system_lang_code") or "en",
                        state.get("lang_pack") or "",
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
