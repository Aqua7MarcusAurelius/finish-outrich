"""
Модуль синхронизации истории (Нагон).

Запускается параллельно с основным циклом воркера. Догружает все
сообщения которые появились в Telegram пока нас не было — включая
медиа (те же путь и формат что у основного хендлера воркера).

Алгоритм:
    1. publish sync.started
    2. Список диалогов — либо берём переданный snapshot (если воркер
       уже вызывал get_dialogs для прогрева кэша), либо запрашиваем
       заново через враппер.
    3. Для каждого диалога:
       - находим в нашей БД max(telegram_message_id)
       - пачками по 100 штук (history_sync.chunk_size из settings)
         тянем iter_messages от самых новых к старым
       - когда все сообщения в батче старее уже записанного — стоп
       - каждое новое сообщение → download media → publish
         message.received (история подхватит как обычно, дубли
         отсечёт unique-индекс)
       - publish sync.dialog.done
    4. publish sync.done

Паузы:
    - 0.5 сек между пачками в одном диалоге
    - 0.5 сек между диалогами
    - FloodWaitError → ждём указанное время + 1, публикуем system.error

См. docs/history_sync.md.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon.errors import FloodWaitError

from core import bus, db
from core import minio as minio_mod
from core.events import EventType, Module, Status
from modules.worker.wrapper import (
    SessionExpired,
    TelegramWrapper,
    build_storage_key,
    detect_media_info,
    serialize_message,
)

log = logging.getLogger(__name__)

SYSTEM_USER_ID = 777000
DEFAULT_CHUNK_SIZE = 100
PAUSE_BETWEEN_CHUNKS = 0.5
PAUSE_BETWEEN_DIALOGS = 0.5


async def _get_chunk_size() -> int:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT value FROM settings WHERE key='history_sync.chunk_size'"
        )
    try:
        return int(val) if val else DEFAULT_CHUNK_SIZE
    except Exception:
        return DEFAULT_CHUNK_SIZE


class HistorySyncService:
    """
    Один инстанс на один воркер. Создаётся из Worker.run().
    Живёт как отдельная asyncio.Task, завершается когда нагон
    отработал или при отмене (Worker.stop).
    """

    def __init__(
        self,
        *,
        account_id: int,
        wrapper: TelegramWrapper,
        dialogs_snapshot: list[dict[str, Any]] | None = None,
    ):
        self.account_id = account_id
        self.wrapper = wrapper
        self.dialogs_snapshot = dialogs_snapshot

    async def run(self) -> str | None:
        """
        Возвращает id события sync.started — на случай если Worker
        хочет его куда-то проассоциировать. None если упали рано.
        """
        started_event = await bus.publish(
            module=Module.HISTORY_SYNC,
            type=EventType.SYNC_STARTED,
            status=Status.IN_PROGRESS,
            account_id=self.account_id,
            data={},
        )
        parent_id = started_event["id"]
        log.info("history_sync: started account=%s", self.account_id)

        try:
            dialogs = self.dialogs_snapshot
            if dialogs is None:
                dialogs = await self.wrapper.get_dialogs(limit=None)

            total_messages = 0
            total_dialogs = 0

            for dlg in dialogs:
                tg_user_id = dlg.get("telegram_user_id")
                if tg_user_id is None or tg_user_id == SYSTEM_USER_ID:
                    continue

                try:
                    count = await self._sync_dialog(dlg, parent_id)
                except asyncio.CancelledError:
                    raise
                except SessionExpired:
                    raise
                except Exception as e:
                    log.exception(
                        "history_sync: dialog %s failed", tg_user_id,
                    )
                    await bus.publish(
                        module=Module.HISTORY_SYNC,
                        type=EventType.SYNC_DIALOG_DONE,
                        status=Status.ERROR,
                        parent_id=parent_id,
                        account_id=self.account_id,
                        data={
                            "telegram_user_id": tg_user_id,
                            "error": str(e),
                        },
                    )
                    continue

                total_messages += count
                total_dialogs += 1

                await bus.publish(
                    module=Module.HISTORY_SYNC,
                    type=EventType.SYNC_DIALOG_DONE,
                    status=Status.SUCCESS,
                    parent_id=parent_id,
                    account_id=self.account_id,
                    data={
                        "telegram_user_id": tg_user_id,
                        "new_messages": count,
                    },
                )

                await asyncio.sleep(PAUSE_BETWEEN_DIALOGS)

            await bus.publish(
                module=Module.HISTORY_SYNC,
                type=EventType.SYNC_DONE,
                status=Status.SUCCESS,
                parent_id=parent_id,
                account_id=self.account_id,
                data={
                    "dialogs_synced": total_dialogs,
                    "messages_synced": total_messages,
                },
            )
            log.info(
                "history_sync: done account=%s dialogs=%d messages=%d",
                self.account_id, total_dialogs, total_messages,
            )
            return parent_id

        except asyncio.CancelledError:
            log.info("history_sync: cancelled account=%s", self.account_id)
            raise
        except SessionExpired:
            # Враппер уже опубликовал account.session_expired. Просто
            # сообщаем что нагон остановлен и выходим. Воркер упадёт
            # по той же причине через основной цикл.
            await bus.publish(
                module=Module.HISTORY_SYNC,
                type=EventType.SYNC_DONE,
                status=Status.ERROR,
                parent_id=parent_id,
                account_id=self.account_id,
                data={"error": "session_expired"},
            )
            raise
        except Exception as e:
            log.exception("history_sync: fatal error account=%s", self.account_id)
            await bus.publish(
                module=Module.HISTORY_SYNC,
                type=EventType.SYSTEM_ERROR,
                status=Status.ERROR,
                account_id=self.account_id,
                data={"message": f"history_sync fatal: {e}"},
            )
            await bus.publish(
                module=Module.HISTORY_SYNC,
                type=EventType.SYNC_DONE,
                status=Status.ERROR,
                parent_id=parent_id,
                account_id=self.account_id,
                data={"error": str(e)},
            )
            return parent_id

    # ─── Нагон одного диалога ──────────────────────────────────────

    async def _sync_dialog(self, dlg_snapshot: dict, parent_id: str) -> int:
        tg_user_id = dlg_snapshot["telegram_user_id"]

        # 1. Максимум уже записанного по этому диалогу
        pool = db.get_pool()
        async with pool.acquire() as conn:
            last_tg = await conn.fetchval(
                """
                SELECT MAX(m.telegram_message_id)
                FROM messages m
                JOIN dialogs d ON d.id = m.dialog_id
                WHERE d.account_id = $1 AND d.telegram_user_id = $2
                """,
                self.account_id, tg_user_id,
            )
        last_tg = last_tg or 0

        # 2. Профиль собеседника из снимка get_dialogs — в том же формате
        # что extract_user_profile, чтобы модуль истории обработал одинаково
        peer_profile = {
            "telegram_user_id": tg_user_id,
            "username": dlg_snapshot.get("username"),
            "first_name": dlg_snapshot.get("first_name"),
            "last_name": dlg_snapshot.get("last_name"),
            "phone": dlg_snapshot.get("phone"),
            "is_bot": bool(dlg_snapshot.get("is_bot", False)),
            "is_contact": bool(dlg_snapshot.get("is_contact", False)),
        }

        chunk_size = await _get_chunk_size()
        offset_id = 0
        count = 0

        while True:
            try:
                messages = await self.wrapper.get_history(
                    tg_user_id, limit=chunk_size, offset_id=offset_id,
                )
            except FloodWaitError as e:
                wait = int(getattr(e, "seconds", 30)) + 1
                log.warning(
                    "history_sync: flood wait %ds account=%s dialog=%s",
                    wait, self.account_id, tg_user_id,
                )
                await bus.publish(
                    module=Module.HISTORY_SYNC,
                    type=EventType.SYSTEM_ERROR,
                    status=Status.ERROR,
                    account_id=self.account_id,
                    data={
                        "message": f"FloodWait {wait}s при нагоне dialog={tg_user_id}",
                        "telegram_user_id": tg_user_id,
                        "wait_seconds": wait,
                    },
                )
                await asyncio.sleep(wait)
                continue

            if not messages:
                break

            # Новые — те что свежее того что у нас уже есть
            new_messages = [m for m in messages if getattr(m, "id", 0) > last_tg]
            if not new_messages:
                break

            # Публикуем от старых к новым — чтобы reply корректно резолвилось
            # на уже записанные сообщения. get_history отдаёт от новых к старым.
            for msg in reversed(new_messages):
                await self._publish_message(msg, tg_user_id, peer_profile, parent_id)
                count += 1

            if len(messages) < chunk_size:
                # батч меньше лимита — достигли самого старого
                break

            # Следующий батч — старше. offset_id = самый маленький id в этом батче
            offset_id = min(getattr(m, "id", 0) for m in messages)
            await asyncio.sleep(PAUSE_BETWEEN_CHUNKS)

        return count

    # ─── Публикация одного сообщения (с медиа) ─────────────────────

    async def _publish_message(
        self,
        msg: Any,
        tg_user_id: int,
        peer_profile: dict,
        parent_id: str,
    ) -> None:
        snapshot = serialize_message(msg)
        telegram_message_id = snapshot["telegram_message_id"]

        media_info = detect_media_info(msg)
        media_payload: list[dict] = []

        if media_info is not None and telegram_message_id is not None:
            storage_key = build_storage_key(
                account_id=self.account_id,
                telegram_user_id=tg_user_id,
                telegram_message_id=telegram_message_id,
                ext=media_info["ext"],
            )
            try:
                blob = await self.wrapper.download_media_bytes(msg)
                if blob:
                    await minio_mod.put_object(
                        storage_key, blob,
                        content_type=media_info.get("mime_type"),
                    )
                    entry = {k: v for k, v in media_info.items() if k != "ext"}
                    entry["storage_key"] = storage_key
                    media_payload.append(entry)
                else:
                    log.warning(
                        "history_sync: empty blob account=%s msg=%s",
                        self.account_id, telegram_message_id,
                    )
            except SessionExpired:
                raise
            except Exception as e:
                log.exception(
                    "history_sync: media store failed msg=%s",
                    telegram_message_id,
                )
                await bus.publish(
                    module=Module.HISTORY_SYNC,
                    type=EventType.SYSTEM_ERROR,
                    status=Status.ERROR,
                    account_id=self.account_id,
                    data={
                        "message": f"media store failed при нагоне: {e}",
                        "telegram_message_id": telegram_message_id,
                    },
                )

        await bus.publish(
            module=Module.HISTORY_SYNC,
            type=EventType.MESSAGE_RECEIVED,
            status=Status.SUCCESS,
            parent_id=parent_id,
            account_id=self.account_id,
            data={
                **snapshot,
                "telegram_user_id": tg_user_id,
                "peer_profile": peer_profile,
                "media": media_payload,
            },
        )
