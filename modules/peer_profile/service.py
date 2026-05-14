"""
Модуль расширенного профиля.

Один раз на нового собеседника тянем bio + день рождения через
`GetFullUserRequest` и публикуем `dialog.profile.fetched` — модуль
истории запишет в `dialogs`. Дальше `{partner_bio}` / `{partner_birthday}`
в промптах autochat'а подставляются из БД.

Слушает шину через consumer group "peer-profile-fetcher":
  - dialog.created    → если воркер для этого аккаунта живой,
                        дёрнуть wrapper.fetch_full_profile и опубликовать
                        dialog.profile.fetched
  - (остальные)       → молча ack'аются

Если воркер для аккаунта offline в момент прихода события — bio/birthday
останутся NULL. Это редкий кейс (новые dialog'и создаются по message'ам,
которые приходят через активный воркер), пишем warning и идём дальше.
Принудительной повторной попытки нет: накопление зомби-задач хуже, чем
пустое bio у редкого диалога.

См. docs/event_bus.md → dialog.created / dialog.profile.fetched.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from core import bus
from core.events import EventType, Module, Status
from modules.worker.wrapper import SessionExpired, WrapperError

log = logging.getLogger(__name__)

PEER_PROFILE_GROUP = "peer-profile-fetcher"
PEER_PROFILE_CONSUMER = "peer-profile-fetcher-1"


class PeerProfileService:
    """
    Consumer-loop модуля. Живёт как фоновая задача в lifespan.

    Зависит от worker_manager только через callable `get_wrapper(account_id)`
    — не импортируем класс напрямую, чтобы избежать жёсткой связки и
    облегчить замену в тестах.
    """

    def __init__(self, *, get_wrapper: Callable[[int], Any]) -> None:
        self._get_wrapper = get_wrapper
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        await bus.ensure_group(PEER_PROFILE_GROUP)
        log.info("PeerProfileService loop started")

        while not self._stop_event.is_set():
            try:
                batch = await bus.read_group(
                    PEER_PROFILE_GROUP, PEER_PROFILE_CONSUMER,
                    count=20, block_ms=5000,
                )
                if not batch:
                    continue

                ack_ids: list[str] = []
                for stream_id, event in batch:
                    try:
                        await self._handle(event)
                        ack_ids.append(stream_id)
                        await bus.record_success(PEER_PROFILE_GROUP, stream_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.exception(
                            "peer_profile: failed to handle event %s (type=%s)",
                            event.get("id"), event.get("type"),
                        )
                        force_ack = await bus.record_failure(
                            PEER_PROFILE_GROUP, stream_id, event, e,
                        )
                        if force_ack:
                            ack_ids.append(stream_id)

                if ack_ids:
                    await bus.ack_group(PEER_PROFILE_GROUP, ack_ids)

            except asyncio.CancelledError:
                log.info("PeerProfileService loop cancelled")
                raise
            except Exception:
                log.exception("PeerProfileService loop error")
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stop_event.set()

    # ─── Dispatcher ───────────────────────────────────────────────

    async def _handle(self, event: dict) -> None:
        if event.get("type") != EventType.DIALOG_CREATED:
            return  # остальные события — мимо нас

        await self._on_dialog_created(event)

    # ─── dialog.created ───────────────────────────────────────────

    async def _on_dialog_created(self, event: dict) -> None:
        """
        Запросить расширенный профиль и опубликовать dialog.profile.fetched.

        Если воркер для аккаунта сейчас offline — пропускаем без ошибок
        (одна из штатных ситуаций: новый диалог зарегистрировали, пока
        воркер ещё успел упасть). Профиль останется NULL — оператор увидит
        это в карточке диалога; можно будет вручную инициировать дотяжку
        в будущем (вне scope этого коммита).
        """
        data = event.get("data") or {}
        account_id = event.get("account_id")
        dialog_id = data.get("dialog_id")
        telegram_user_id = data.get("telegram_user_id")

        if account_id is None or dialog_id is None or telegram_user_id is None:
            log.warning(
                "peer_profile: dialog.created with missing fields "
                "(account_id=%s dialog_id=%s tg_user=%s) — skip",
                account_id, dialog_id, telegram_user_id,
            )
            return

        wrapper = self._get_wrapper(account_id)
        if wrapper is None:
            log.info(
                "peer_profile: no live wrapper for account=%s, "
                "skip full-profile fetch for dialog=%s",
                account_id, dialog_id,
            )
            return

        try:
            profile = await wrapper.fetch_full_profile(int(telegram_user_id))
        except SessionExpired:
            # Враппер уже опубликовал account.session_expired — не
            # дублируем. Просто пропускаем; на следующий раз когда
            # сессия будет валидной, нового события dialog.created
            # для этого диалога уже не будет — bio останется NULL.
            log.info(
                "peer_profile: session expired during fetch account=%s dialog=%s",
                account_id, dialog_id,
            )
            return
        except WrapperError as e:
            log.warning(
                "peer_profile: wrapper error account=%s dialog=%s: %s",
                account_id, dialog_id, e,
            )
            return
        except Exception:
            log.exception(
                "peer_profile: fetch_full_profile failed account=%s dialog=%s",
                account_id, dialog_id,
            )
            return

        await bus.publish(
            module=Module.PEER_PROFILE,
            type=EventType.DIALOG_PROFILE_FETCHED,
            status=Status.SUCCESS,
            parent_id=event.get("id"),
            account_id=account_id,
            data={
                "dialog_id": dialog_id,
                "telegram_user_id": telegram_user_id,
                "bio": profile.get("bio"),
                "birthday": profile.get("birthday"),
            },
        )
