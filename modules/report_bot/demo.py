"""Temporary demonstration cards: Redis state only, no production moderation.

The sender binds a random nonce to one bot message with a finite Redis TTL.
This module never creates a Telegram client or touches DB, CRM or event streams.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from weakref import WeakValueDictionary

from telethon import Button
from telethon.errors import MessageNotModifiedError

from core import redis as redis_mod

log = logging.getLogger(__name__)
CHOICES = {"take": "Валид, беру", "no_take": "Валид, не беру", "invalid": "Невалид"}
_CALLBACK = re.compile(r"demo:([0-9a-f]{32}):(take|no_take|invalid)\Z", re.ASCII)
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
EDIT_TIMEOUT_SEC = 10.0


def _guard(nonce: str) -> asyncio.Lock:
    lock = _locks.get(nonce)
    if lock is None:
        lock = asyncio.Lock()
        _locks[nonce] = lock
    return lock


def _buttons(nonce: str, choice: str):
    return [[Button.inline(("✅ " if key == choice else "") + label,
                           f"demo:{nonce}:{key}".encode())
             for key, label in CHOICES.items()]]


async def _answer(event, text: str, *, alert: bool = False) -> None:
    try:
        await asyncio.wait_for(event.answer(text, alert=alert), timeout=10)
    except Exception as exc:
        log.info("demo callback answer unavailable: %s", type(exc).__name__)


async def handle_demo_callback(event, client) -> None:
    """Handle only a bound, unexpired demo card; never fall through to CRM."""
    try:
        raw = (event.data or b"").decode("ascii")
        match = _CALLBACK.fullmatch(raw)
    except (AttributeError, UnicodeDecodeError):
        match = None
    if match is None:
        await _answer(event, "Некорректная тестовая кнопка", alert=True)
        return
    if client is None:
        await _answer(event, "Тест временно недоступен", alert=True)
        return
    nonce, choice = match.groups()
    key = "demo_report:" + nonce
    try:
        async with _guard(nonce):
            redis = redis_mod.get_client()
            payload = await redis.get(key)
            if not payload or await redis.ttl(key) <= 0:
                await _answer(event, "Тест завершён", alert=True)
                return
            try:
                state = json.loads(payload)
                valid = (isinstance(state, dict)
                         and type(state.get("chat_id")) is int
                         and type(state.get("message_id")) is int
                         and state["message_id"] > 0
                         and all(isinstance(state.get(k), str) and 0 < len(state[k]) <= 1024
                                 for k in ("caption_hidden", "caption_revealed")))
            except (TypeError, ValueError):
                valid = False
            if not valid:
                await _answer(event, "Тест завершён", alert=True)
                return
            if (getattr(event, "chat_id", None) != state["chat_id"]
                    or getattr(event, "message_id", None) != state["message_id"]):
                await _answer(event, "Это другая тестовая карточка", alert=True)
                return
            state["choice"] = choice
            # XX prevents resurrection if expiry races this callback. KEEPTTL
            # keeps the sender's original deadline even after repeated clicks.
            saved = await redis.set(key, json.dumps(state, ensure_ascii=False), xx=True, keepttl=True)
            if not saved:
                await _answer(event, "Тест завершён", alert=True)
                return
            caption = state["caption_revealed"] if choice == "take" else state["caption_hidden"]
            try:
                await asyncio.wait_for(client.edit_message(
                    state["chat_id"], state["message_id"], caption,
                    buttons=_buttons(nonce, choice), parse_mode=None,
                ), timeout=EDIT_TIMEOUT_SEC)
            except MessageNotModifiedError:
                pass  # Repeating the same selection is a successful no-op.
            except Exception as exc:
                log.info("demo card edit unavailable: %s", type(exc).__name__)
                await _answer(event, "Выбор сохранён, но карточка не обновилась. Повторите нажатие.", alert=True)
                return
        await _answer(event, f"Тест: {CHOICES[choice]}. CRM не изменяется.")
    except Exception as exc:
        log.warning("demo callback unavailable: %s", type(exc).__name__)
        await _answer(event, "Тест временно недоступен", alert=True)
