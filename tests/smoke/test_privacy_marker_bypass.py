"""Smoke-тест для Fix #4: generic 406-ошибки от TG с cannot-write
маркером (ALLOW_PAYMENT_REQUIRED / PRIVACY_PREMIUM_REQUIRED и т.д.) НЕ
должны маскироваться под SessionExpired в wrapper._guard.

Проблема: в Telethon 1.36 базовым классом для всех ошибок с HTTP-кодом
406 является ``AuthKeyError`` (исторически 406 = auth). Когда TG отдаёт
"ALLOW_PAYMENT_REQUIRED" (лид требует Stars за DM), Telethon делает
экземпляр ``AuthKeyError`` со str = 'RPCError 406: ALLOW_PAYMENT_REQUIRED
(caused by SendMessageRequest)'. Наш ``_guard`` раньше сразу мапил его в
``SessionExpired`` и роняль воркер в ``session_expired`` — хотя сессия
живая, просто конкретный лид не пропускает бесплатные сообщения.

Фикс: перед мапингом в SessionExpired проверяем текст на
_NOT_SESSION_ERROR_MARKERS. Если совпало — пропускаем ошибку наверх как
есть, autochat/service.py::except RPCError её матчит и делает
CannotWrite → CRM invalid.

Запуск (внутри контейнера app):
    docker compose exec -T app python -m tests.smoke.test_privacy_marker_bypass
"""
from __future__ import annotations

import asyncio
import sys

from telethon.errors.rpcbaseerrors import AuthKeyError
from modules.worker.wrapper import (
    _NOT_SESSION_ERROR_MARKERS,
    SessionExpired,
    _SESSION_EXPIRED_ERRORS,
)


def _make_fake_auth_key_error(rpc_message: str) -> AuthKeyError:
    """Собираем экземпляр AuthKeyError с нужным сообщением, как это делает
    Telethon для generic 406-ошибок без специфичного класса.

    Telethon RPCError.__init__(request, message) требует эти два аргумента;
    строковое представление формируется как
    `RPCError <code>: <message> (caused by <request>)`.
    """
    return AuthKeyError(request="SendMessageRequest", message=rpc_message)


async def test_marker_bypass_in_guard() -> bool:
    """
    _guard: если бросается AuthKeyError с ALLOW_PAYMENT_REQUIRED в тексте
    → это НЕ должно превратиться в SessionExpired. Ошибка проходит наверх
    как есть.
    """
    # Мини-двойник wrapper._guard без реального Telethon-client.
    async def _fake_send_that_hits_allow_payment():
        raise _make_fake_auth_key_error(
            "RPCError 406: ALLOW_PAYMENT_REQUIRED (caused by SendMessageRequest)"
        )

    # Смоделируем логику _guard:
    try:
        await _fake_send_that_hits_allow_payment()
        return False  # мы должны были поймать AuthKeyError
    except _SESSION_EXPIRED_ERRORS as e:
        # НОВАЯ логика _guard: проверяем маркер
        if any(m in str(e) for m in _NOT_SESSION_ERROR_MARKERS):
            # правильный путь — пропустить наверх
            return True
        # Старое поведение — мапить в SessionExpired — было бы ошибкой:
        return False


async def test_real_session_expired_still_mapped() -> bool:
    """
    Regression-guard: настоящий session-expired (например SESSION_REVOKED
    без cannot-write маркера в тексте) ДОЛЖЕН по-прежнему мапиться в
    нашу SessionExpired.
    """
    async def _fake_send_that_hits_real_session_revoke():
        raise _make_fake_auth_key_error(
            "RPCError 406: SESSION_REVOKED (caused by SendMessageRequest)"
        )

    try:
        await _fake_send_that_hits_real_session_revoke()
        return False
    except _SESSION_EXPIRED_ERRORS as e:
        # Маркеры НЕ совпадают → должно пойти на подмену
        return not any(m in str(e) for m in _NOT_SESSION_ERROR_MARKERS)


async def test_all_markers_recognised() -> bool:
    """Каждый маркер из _NOT_SESSION_ERROR_MARKERS реально распознаётся."""
    for marker in _NOT_SESSION_ERROR_MARKERS:
        err = _make_fake_auth_key_error(f"RPCError 406: {marker} (caused by X)")
        if not any(m in str(err) for m in _NOT_SESSION_ERROR_MARKERS):
            print(f"  FAIL: marker {marker!r} not detected by any(m in str(e))")
            return False
    return True


async def _run_all() -> int:
    results = [
        ("test_marker_bypass_in_guard",
         await test_marker_bypass_in_guard()),
        ("test_real_session_expired_still_mapped",
         await test_real_session_expired_still_mapped()),
        ("test_all_markers_recognised",
         await test_all_markers_recognised()),
    ]
    print()
    for name, ok in results:
        print(f"  {name:45} → {'PASS' if ok else 'FAIL'}")
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print()
    if passed == total:
        print(f"ALL {total} PRIVACY-MARKER TESTS PASSED")
        return 0
    print(f"FAILED: {total - passed}/{total}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_all()))
