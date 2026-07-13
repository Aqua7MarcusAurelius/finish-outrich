"""
Доставка success-отчётов в Telegram-группы выгрузки (этап 10.20).

Надстройка над уже существующим потоком отчётов: `ReportConsumer` (10.8)
кладёт HTML в MinIO и публикует `autochat.report_saved {account_id,
minio_key, folder}`. Этот сервис ловит событие, и если `folder == "success"`,
сервисным Telegram-аккаунтом заливает тот же файл в группу выгрузки воркера.

Группа определяется так: `account_id` → имя воркера (accounts.name) →
«семейство» (префикс имени до '_', напр. SAD_03_RU → SAD) → `chat_id` из
карты `REPORT_GROUP_MAP` в .env. Одна группа обслуживает всех воркеров
семейства; новые воркеры подхватываются без правки карты.

Сервис держит СВОЁ подключение к сервис-аккаунту (не через WorkerManager):
админскому аккаунту не нужны подписки на входящие и нагон истории, только
отправка файлов. Поэтому поднимаем голый TelegramWrapper, прогреваем
entity-кэш одним get_dialogs (чтобы send_file(chat_id) резолвил группы) и
слушаем шину.

Дедуп — флаг `report_delivered:{dialog_id}` в Redis (SETNX): одна выгрузка
на диалог. На ошибке отправки флаг снимаем и публикуем report.delivery_failed
(без вечного pending — событие ack-аем, оператор видит сбой в ленте).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from core import bus, db, minio
from core import redis as redis_mod
from core.config import settings
from core.events import EventType, Module, Status
from modules.worker.wrapper import (
    ProxyUnavailable,
    SessionExpired,
    TelegramWrapper,
)

log = logging.getLogger(__name__)

GROUP = "report-delivery"
CONSUMER = "report-delivery-1"

# Дедуп-флаг живёт долго: повторный dialog.status_changed по тому же
# диалогу не должен заново слать файл. 90 дней с запасом перекрывают
# любой реалистичный лаг повторного события; вечные ключи не копим.
_DEDUP_TTL_SEC = 90 * 24 * 3600

# Пауза между попытками переподключения сервис-аккаунта.
# M1 (2026-07-13): экспоненциальный бэкофф от базовой до максимальной паузы —
# при мёртвом прокси сервис-аккаунта раньше долбили connect каждые 30с и
# спамили шину ~2880 system.error/сутки. Теперь пауза растёт до _RECONNECT_MAX_SEC.
_RECONNECT_DELAY_SEC = 30
_RECONNECT_MAX_SEC = 900

# Telegram режет подпись на 1024 символах — держимся под лимитом.
_CAPTION_MAX = 1024


class ReportDeliveryService:
    """Bus-consumer: success-отчёт из MinIO → группа выгрузки воркера."""

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._wrapper: TelegramWrapper | None = None
        self._service_account_id: int | None = None
        self._group_map: dict[str, int] = {}

    # ─── Жизненный цикл ────────────────────────────────────────────

    async def run(self) -> None:
        # Ключи карты — «семейства» воркеров (префикс имени до '_'),
        # нормализуем к ВЕРХНЕМУ регистру: в .env можно писать как удобно.
        self._group_map = {
            str(k).upper(): v for k, v in settings.report_group_map.items()
        }
        raw = (settings.REPORT_GROUP_MAP or "").strip()
        if raw and not self._group_map:
            log.warning(
                "report_delivery: REPORT_GROUP_MAP задан, но распарсился "
                "пустым — проверьте JSON. Доставка выключена."
            )
        if not self._group_map:
            log.info(
                "report_delivery: карта групп пуста — доставка выключена "
                "(заполните REPORT_GROUP_MAP в .env)."
            )
            return

        account = await self._load_service_account()
        if account is None:
            log.warning(
                "report_delivery: сервис-аккаунт name='%s' не найден — "
                "доставка выключена. Авторизуйте его мастером «новый аккаунт».",
                settings.SERVICE_ACCOUNT_NAME,
            )
            return
        if not account.get("session_data"):
            log.warning(
                "report_delivery: сервис-аккаунт name='%s' не авторизован "
                "(нет session_data) — доставка выключена.",
                settings.SERVICE_ACCOUNT_NAME,
            )
            return
        self._service_account_id = int(account["id"])

        await bus.ensure_group(GROUP, start_id="$")  # только новые success
        log.info(
            "ReportDelivery loop started: %d групп в карте, сервис-аккаунт id=%s",
            len(self._group_map), self._service_account_id,
        )

        reconnect_delay = _RECONNECT_DELAY_SEC
        while not self._stop_event.is_set():
            try:
                if self._wrapper is None or not self._wrapper.is_connected():
                    if not await self._connect():
                        # M1: экспоненциальный бэкофф вместо фиксированных 30с.
                        await self._sleep(reconnect_delay)
                        reconnect_delay = min(
                            reconnect_delay * 2, _RECONNECT_MAX_SEC,
                        )
                        continue
                    reconnect_delay = _RECONNECT_DELAY_SEC  # успех — сброс

                batch = await bus.read_group(
                    GROUP, CONSUMER, count=20, block_ms=5000,
                )
                if not batch:
                    continue

                ack_ids: list[str] = []
                for stream_id, event in batch:
                    try:
                        await self._handle(event)
                        ack_ids.append(stream_id)
                        await bus.record_success(GROUP, stream_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.exception(
                            "report_delivery: failed event %s", event.get("id"),
                        )
                        force_ack = await bus.record_failure(
                            GROUP, stream_id, event, e,
                        )
                        if force_ack:
                            ack_ids.append(stream_id)
                if ack_ids:
                    await bus.ack_group(GROUP, ack_ids)

            except asyncio.CancelledError:
                log.info("ReportDelivery cancelled")
                raise
            except Exception:
                log.exception("ReportDelivery loop error")
                await self._sleep(1)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._wrapper is not None:
            try:
                await self._wrapper.disconnect()
            except Exception:
                log.exception("report_delivery: disconnect error on stop")
            finally:
                self._wrapper = None

    async def _sleep(self, seconds: float) -> None:
        """Прерываемый sleep — мгновенно выходит при stop()."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ─── Подключение сервис-аккаунта ───────────────────────────────

    async def _connect(self) -> bool:
        """
        Поднять/переподнять подключение сервис-аккаунта. Best-effort:
        при неудаче логируем и возвращаем False (run() поспит и повторит).
        """
        account = await self._load_service_account()
        if account is None or not account.get("session_data"):
            log.warning("report_delivery: сервис-аккаунт пропал/разлогинен")
            return False

        try:
            wrapper = TelegramWrapper(
                account_id=int(account["id"]),
                api_id=settings.TELEGRAM_API_ID,
                api_hash=settings.TELEGRAM_API_HASH,
                session_data=account["session_data"],
                proxy_primary=account["proxy_primary"],
                proxy_fallback=account["proxy_fallback"],
            )
            # M1: publish_error=False — не спамим шину system.error на каждой
            # попытке; логируем сами (run() ретраит с экспо-бэкоффом).
            await wrapper.connect(require_authorized=True, publish_error=False)
        except (SessionExpired, ProxyUnavailable) as e:
            log.error("report_delivery: connect failed: %s", e)
            return False
        except Exception:
            log.exception("report_delivery: connect failed (unexpected)")
            return False

        # Прогрев entity-кэша: StringSession не помнит access_hash групп между
        # рестартами — без этого send_file(chat_id) упадёт на резолве.
        try:
            await wrapper.get_dialogs(limit=None)
        except Exception:
            log.warning(
                "report_delivery: не удалось прогреть entity-кэш "
                "(send_file по chat_id может не зарезолвиться)",
            )

        self._wrapper = wrapper
        log.info("report_delivery: сервис-аккаунт подключён")
        return True

    # ─── Обработка события ─────────────────────────────────────────

    async def _handle(self, event: dict[str, Any]) -> None:
        if event.get("type") != EventType.AUTOCHAT_REPORT_SAVED:
            return
        data = event.get("data") or {}
        if data.get("folder") != "success":
            return  # шлём только удачные

        dialog_id = data.get("dialog_id")
        minio_key = data.get("minio_key")
        account_id = event.get("account_id")
        if not dialog_id or not minio_key or account_id is None:
            log.warning(
                "report_delivery: неполное событие (dialog_id=%s key=%s acc=%s)",
                dialog_id, minio_key, account_id,
            )
            return

        worker_name = await self._worker_name(int(account_id))
        if worker_name is None:
            log.warning(
                "report_delivery: воркер account_id=%s не найден", account_id,
            )
            return

        # Группа — по «семейству» воркера: префикс имени до первого '_'
        # (SAD_03_RU → SAD). Одна группа обслуживает всех воркеров семейства.
        family = worker_name.split("_", 1)[0].upper()
        chat_id = self._group_map.get(family)
        if chat_id is None:
            log.info(
                "report_delivery: для семейства %s (воркер %s) нет группы — пропуск",
                family, worker_name,
            )
            await bus.publish(
                module=Module.REPORT_DELIVERY,
                type=EventType.REPORT_DELIVERY_SKIPPED,
                status=Status.SUCCESS,
                account_id=int(account_id),
                data={
                    "dialog_id": int(dialog_id),
                    "worker": worker_name,
                    "family": family,
                    "reason": "no_group_mapped",
                    "minio_key": minio_key,
                },
            )
            return

        # Дедуп: занимаем диалог. Если уже доставляли — выходим молча.
        redis = redis_mod.get_client()
        dedup_key = f"report_delivered:{int(dialog_id)}"
        claimed = await redis.set(dedup_key, b"1", nx=True, ex=_DEDUP_TTL_SEC)
        if not claimed:
            log.debug(
                "report_delivery: диалог %s уже доставлен — пропуск", dialog_id,
            )
            return

        try:
            blob = await minio.get_object(
                minio_key, bucket=settings.MINIO_REPORTS_BUCKET,
            )
            caption = await self._build_caption(int(dialog_id), worker_name)
            filename = minio_key.rsplit("/", 1)[-1]
            result = await self._wrapper.send_file(  # type: ignore[union-attr]
                chat_id, blob, filename=filename, caption=caption,
            )
        except SessionExpired as e:
            # Сессия сервис-аккаунта протухла прямо на отправке: снимаем
            # claim, роняем подключение (run() переподнимет), публикуем сбой.
            await self._release(redis, dedup_key)
            if self._wrapper is not None:
                try:
                    await self._wrapper.disconnect()
                except Exception:
                    pass
                self._wrapper = None
            await self._publish_failed(
                int(account_id), int(dialog_id), worker_name, chat_id,
                minio_key, f"session_expired: {e}",
            )
            return
        except Exception as e:
            # Любой иной сбой (FloodWait, нерезолвящийся chat, MinIO и т.п.):
            # снимаем claim, публикуем сбой, ack-аем (не виснем в pending).
            await self._release(redis, dedup_key)
            log.exception(
                "report_delivery: доставка диалога %s не удалась", dialog_id,
            )
            await self._publish_failed(
                int(account_id), int(dialog_id), worker_name, chat_id,
                minio_key, str(e)[:300],
            )
            return

        await bus.publish(
            module=Module.REPORT_DELIVERY,
            type=EventType.REPORT_DELIVERED,
            status=Status.SUCCESS,
            account_id=int(account_id),
            data={
                "dialog_id": int(dialog_id),
                "worker": worker_name,
                "chat_id": chat_id,
                "minio_key": minio_key,
                "telegram_message_id": result.get("telegram_message_id"),
            },
        )
        log.info(
            "report_delivery: доставлен dialog=%s worker=%s chat=%s",
            dialog_id, worker_name, chat_id,
        )

    async def _release(self, redis: Any, dedup_key: str) -> None:
        """Снять дедуп-claim, чтобы доставку можно было повторить позже."""
        try:
            await redis.delete(dedup_key)
        except Exception:
            log.debug("report_delivery: не удалось снять claim %s", dedup_key)

    async def _publish_failed(
        self,
        account_id: int,
        dialog_id: int,
        worker: str,
        chat_id: int,
        minio_key: str,
        reason: str,
    ) -> None:
        try:
            await bus.publish(
                module=Module.REPORT_DELIVERY,
                type=EventType.REPORT_DELIVERY_FAILED,
                status=Status.ERROR,
                account_id=account_id,
                data={
                    "dialog_id": dialog_id,
                    "worker": worker,
                    "chat_id": chat_id,
                    "minio_key": minio_key,
                    "reason": reason,
                },
            )
        except Exception:
            log.exception("report_delivery: не смог опубликовать failed")

    # ─── Данные ────────────────────────────────────────────────────

    async def _load_service_account(self) -> dict[str, Any] | None:
        """Строка сервис-аккаунта по имени из SERVICE_ACCOUNT_NAME."""
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, name, session_data, proxy_primary, proxy_fallback
                FROM accounts WHERE name = $1
                ORDER BY id LIMIT 1
                """,
                settings.SERVICE_ACCOUNT_NAME,
            )
        return dict(row) if row is not None else None

    async def _worker_name(self, account_id: int) -> str | None:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT name FROM accounts WHERE id = $1", account_id,
            )

    async def _build_caption(self, dialog_id: int, worker_name: str) -> str:
        """
        Подробная подпись к файлу: воркер, собеседник, поля оценщика, время.
        Только непустые строки. Итог режется до лимита подписи Telegram.
        """
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    d.username, d.first_name, d.last_name, d.telegram_user_id,
                    e.extracted_name, e.extracted_age, e.extracted_city,
                    e.extracted_workplace, e.extracted_interests, e.evaluated_at
                FROM dialogs d
                LEFT JOIN lead_evaluations e ON e.dialog_id = d.id
                WHERE d.id = $1
                """,
                dialog_id,
            )
        ctx = dict(row) if row is not None else {}

        # Собеседник: '@uname (Имя Фамилия)' либо 'id12345'
        uname = ctx.get("username")
        full = " ".join(
            p for p in (ctx.get("first_name"), ctx.get("last_name")) if p
        ).strip()
        if uname:
            partner = f"@{uname}" + (f" ({full})" if full else "")
        elif ctx.get("telegram_user_id") is not None:
            partner = f"id{ctx['telegram_user_id']}" + (f" ({full})" if full else "")
        else:
            partner = full or "—"

        lines: list[str] = [
            f"✅ Успешный диалог — {worker_name}",
            f"Собеседник: {partner}",
        ]

        eval_fields = [
            ("Имя", ctx.get("extracted_name")),
            ("Возраст", ctx.get("extracted_age")),
            ("Город", ctx.get("extracted_city")),
            ("Работа", ctx.get("extracted_workplace")),
            ("Интересы", ctx.get("extracted_interests")),
        ]
        rendered = [
            f"{label}: {str(value).strip()}"
            for label, value in eval_fields
            if value is not None and str(value).strip()
        ]
        if rendered:
            lines.append("—")
            lines.extend(rendered)

        finished = ctx.get("evaluated_at")
        if isinstance(finished, datetime):
            lines.append("—")
            lines.append(
                "Завершён: "
                + finished.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            )

        return "\n".join(lines)[:_CAPTION_MAX]
