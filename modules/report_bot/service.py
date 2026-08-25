"""
Модерация успешных диалогов кнопками в TG-группе (2026-08-23).

Раньше `report_delivery` слал HTML-отчёт сервис-аккаунтом, а
`partner_crm/finalizer` независимо пушил `ready_for_transfer` в CRM —
человек в решении не участвовал. Ошибку оценщика отыграть было нельзя:
API партнёра запрещает менять статус лида, попавшего в финал.

Теперь между «оценщик сказал success» и «статус ушёл партнёру» стоит
человек:

    оценщик: success
        ↓  пузырь «Модерация» (не терминальный → finalizer молчит)
    бот шлёт отчёт + 3 кнопки в группу семейства
        ↓  ждём сколько угодно — без нажатия ничего не происходит
    нажатие → ✅ у кнопки, старт 4-часового таймера
        ↓  перевыбор сдвигает таймер (считаем от последнего)
    таймер истёк → пуш в CRM → кнопки снимаются,
                   в подпись дописывается итог, пузырь → терминальный

Почему бот, а не сервис-аккаунт: inline-кнопки в MTProto доступны
только ботам, юзер-аккаунт `ReplyInlineMarkup` отправить не может.

Почему таймер в БД, а не в памяти: рестарт app не должен ни терять
отложенные пуши, ни выстреливать ими повторно.

Три параллельные задачи внутри сервиса:
  * `run()`          — bus-consumer `autochat.report_saved(folder=success)`
  * `events.CallbackQuery` — обработчик нажатий (вешается на клиент)
  * `_ticker_loop()` — раз в минуту добивает просроченные

Owns: `report_moderation` (INSERT/UPDATE). Читает: `report_families`,
`dialogs`, `lead_evaluations`, `accounts`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon import Button, TelegramClient, events
from telethon.sessions import StringSession

from core import bus, db, minio
from core import redis as redis_mod
from core.config import settings
from core.events import EventType, Module, Status
from modules.dialog_statuses import service as dialog_statuses_service
from modules.partner_crm.errors import PartnerCrmError
from modules.partner_crm.service import PartnerCrmService

log = logging.getLogger(__name__)

GROUP = "report-bot"
CONSUMER = "report-bot-1"

# Сколько ждём после последнего нажатия, прежде чем отправить статус
# партнёру. Окно на передумать: перевыбор сдвигает отсчёт заново.
MODERATION_DELAY_SEC = 4 * 3600

# Как часто тикер ищет просроченные. Минута — компромисс между
# точностью (±60с на 4-часовом окне незаметно) и нагрузкой на БД.
TICKER_INTERVAL_SEC = 60

#: Сколько раз пробуем пушить статус, если CRM отвечает ошибкой. Исчерпав
#: лимит, строка выпадает из выборки тикера — иначе постоянный отказ
#: (403 на лиде, уже финализированном партнёром) крутится вечно раз в минуту.
MAX_PUSH_ATTEMPTS = 3
#: Пауза перед повторной попыткой: 15 мин, затем 1 ч. Разнесены широко
#: специально — три попытки подряд с короткими паузами сгорели бы за
#: полчаса и не пережили бы обычную недоступность CRM.
_PUSH_BACKOFF_SEC = (15 * 60, 60 * 60)

# Карту семейств перечитываем на лету: новое семейство добавляется
# INSERT'ом в report_families и подхватывается без рестарта app
# (в .env-эпоху это стоило нам двух пересборок — EKSTAZ и ALADIN).
FAMILIES_TTL_SEC = 60.0

_RECONNECT_DELAY_SEC = 15
_RECONNECT_MAX_SEC = 300

# Дедуп отправки отчёта: одно сообщение на диалог, даже если событие
# прилетит повторно (переигранный стрим, ручной rebuild отчёта).
_DEDUP_TTL_SEC = 90 * 24 * 3600

_CAPTION_MAX = 1024

# Подписи кнопок и то, что уходит в CRM. `take` требует l2_login
# семейства — без него деградирует до ready_for_transfer (см.
# PartnerCrmService.push_lead_moderated).
CHOICES: dict[str, str] = {
    "take":    "Валид, беру",
    "no_take": "Валид, не беру",
    "invalid": "Невалид",
}

# Какой пузырь ставим в нашей админке после пуша. Отражает решение
# модератора, а не буквальный статус CRM: при `take` партнёр увидит
# `transferred`, у нас останется «Завершен успешно».
_BUBBLE_BY_CHOICE = {
    "take":    "Завершен успешно",
    "no_take": "Завершен успешно",
    "invalid": "Невалид",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ReportBotService:
    """Бот-клиент + модерация. Живёт синглтоном в lifespan."""

    def __init__(self, partner_crm: PartnerCrmService) -> None:
        self._crm = partner_crm
        self._client: TelegramClient | None = None
        self._stop_event = asyncio.Event()
        self._ticker_task: asyncio.Task | None = None
        self._families: dict[str, dict[str, Any]] = {}
        self._families_at: float = 0.0

    # ─── Жизненный цикл ──────────────────────────────────────────────

    def is_configured(self) -> bool:
        return bool(getattr(settings, "REPORT_BOT_TOKEN", ""))

    async def run(self) -> None:
        if not self.is_configured():
            log.info(
                "report_bot: REPORT_BOT_TOKEN не задан — модерация выключена, "
                "успехи останутся в пузыре «Модерация» без отправки в CRM",
            )
            return

        await self._load_families(force=True)
        if not self._families:
            log.warning(
                "report_bot: report_families пуста — некуда слать отчёты",
            )

        # start_id='$' — только новые события. Иначе после первого
        # запуска бот выгрузит в группы весь исторический бэклог.
        await bus.ensure_group(GROUP, start_id="$")
        log.info(
            "report_bot: старт, семейств в карте %d, задержка пуша %dч",
            len(self._families), MODERATION_DELAY_SEC // 3600,
        )

        reconnect_delay = _RECONNECT_DELAY_SEC
        while not self._stop_event.is_set():
            try:
                if self._client is None or not self._client.is_connected():
                    if not await self._connect():
                        await self._sleep(reconnect_delay)
                        reconnect_delay = min(
                            reconnect_delay * 2, _RECONNECT_MAX_SEC,
                        )
                        continue
                    reconnect_delay = _RECONNECT_DELAY_SEC

                batch = await bus.read_group(
                    GROUP, CONSUMER, count=20, block_ms=5000,
                )
                if not batch:
                    continue

                ack_ids: list[str] = []
                for stream_id, event in batch:
                    try:
                        await self._handle_report(event)
                        ack_ids.append(stream_id)
                        await bus.record_success(GROUP, stream_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.exception(
                            "report_bot: событие %s не обработано",
                            event.get("id"),
                        )
                        force_ack = await bus.record_failure(
                            GROUP, stream_id, event, e,
                        )
                        if force_ack:
                            ack_ids.append(stream_id)
                if ack_ids:
                    await bus.ack_group(GROUP, ack_ids)

            except asyncio.CancelledError:
                log.info("report_bot: cancelled")
                raise
            except Exception:
                log.exception("report_bot: ошибка основного цикла")
                await self._sleep(1)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ticker_task is not None and not self._ticker_task.done():
            self._ticker_task.cancel()
            try:
                await self._ticker_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                log.exception("report_bot: ошибка disconnect")
            finally:
                self._client = None

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _connect(self) -> bool:
        """Поднять бот-клиент и повесить обработчик нажатий."""
        try:
            client = TelegramClient(
                StringSession(),
                settings.TELEGRAM_API_ID,
                settings.TELEGRAM_API_HASH,
            )
            await client.start(bot_token=settings.REPORT_BOT_TOKEN)
            me = await client.get_me()
        except Exception:
            log.exception("report_bot: не удалось подключить бота")
            return False

        client.add_event_handler(self._on_callback, events.CallbackQuery)
        self._client = client
        log.info("report_bot: подключён как @%s", getattr(me, "username", "?"))

        # Тикер стартуем один раз — он переживает переподключения клиента.
        if self._ticker_task is None or self._ticker_task.done():
            self._ticker_task = asyncio.create_task(
                self._ticker_loop(), name="report-bot-ticker",
            )
        return True

    # ─── Карта семейств ──────────────────────────────────────────────

    async def _load_families(self, *, force: bool = False) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if not force and (now - self._families_at) < FAMILIES_TTL_SEC:
            return
        pool = db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT family, chat_id, l2_login FROM report_families "
                "WHERE is_active",
            )
        self._families = {
            r["family"].upper(): {
                "chat_id": int(r["chat_id"]),
                "l2_login": r["l2_login"],
            }
            for r in rows
        }
        self._families_at = now

    # ─── Отправка отчёта с кнопками ──────────────────────────────────

    async def _handle_report(self, event: dict[str, Any]) -> None:
        if event.get("type") != EventType.AUTOCHAT_REPORT_SAVED:
            return
        data = event.get("data") or {}
        if data.get("folder") != "success":
            return

        dialog_id = data.get("dialog_id")
        minio_key = data.get("minio_key")
        account_id = event.get("account_id")
        if not dialog_id or not minio_key or account_id is None:
            log.warning(
                "report_bot: неполное событие (dialog=%s key=%s acc=%s)",
                dialog_id, minio_key, account_id,
            )
            return

        dialog_id = int(dialog_id)
        account_id = int(account_id)

        info = await self._dialog_info(dialog_id, account_id)
        if info is None:
            return
        worker_name = info["worker_name"]
        username = info["username"]

        family = worker_name.split("_", 1)[0].upper()
        await self._load_families()
        fam = self._families.get(family)
        if fam is None:
            log.info(
                "report_bot: семейства %s нет в report_families — пропуск "
                "(воркер %s, диалог %s)", family, worker_name, dialog_id,
            )
            await bus.publish(
                module=Module.REPORT_DELIVERY,
                type=EventType.REPORT_DELIVERY_SKIPPED,
                status=Status.SUCCESS,
                account_id=account_id,
                data={
                    "dialog_id": dialog_id, "worker": worker_name,
                    "family": family, "reason": "no_group_mapped",
                    "minio_key": minio_key,
                },
            )
            return

        if not username:
            log.warning(
                "report_bot: диалог %s без username — модерировать нечего",
                dialog_id,
            )
            return

        # Дедуп: одно сообщение на диалог. Redis, а не БД — событие
        # может прилететь раньше, чем строка модерации появится.
        redis = redis_mod.get_client()
        dedup_key = f"report_moderation_sent:{dialog_id}"
        claimed = await redis.set(dedup_key, b"1", nx=True, ex=_DEDUP_TTL_SEC)
        if not claimed:
            log.debug("report_bot: диалог %s уже отправлен", dialog_id)
            return

        try:
            blob = await minio.get_object(
                minio_key, bucket=settings.MINIO_REPORTS_BUCKET,
            )
            caption = await self._build_caption(dialog_id, worker_name)
            sent = await self._client.send_file(   # type: ignore[union-attr]
                fam["chat_id"],
                blob,
                file_name=minio_key.rsplit("/", 1)[-1],
                caption=caption,
                buttons=self._buttons(dialog_id, chosen=None),
            )
        except Exception as e:
            # Не смогли отправить — снимаем claim, чтобы следующая
            # попытка (ручная или переигранное событие) сработала.
            await redis.delete(dedup_key)
            log.exception(
                "report_bot: отправка отчёта по диалогу %s не удалась",
                dialog_id,
            )
            await bus.publish(
                module=Module.REPORT_DELIVERY,
                type=EventType.REPORT_DELIVERY_FAILED,
                status=Status.ERROR,
                account_id=account_id,
                data={
                    "dialog_id": dialog_id, "worker": worker_name,
                    "chat_id": fam["chat_id"], "minio_key": minio_key,
                    "reason": str(e)[:300],
                },
            )
            return

        pool = db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO report_moderation
                    (dialog_id, username, account_id, family,
                     chat_id, message_id, minio_key)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (dialog_id) DO UPDATE
                    SET chat_id    = EXCLUDED.chat_id,
                        message_id = EXCLUDED.message_id,
                        minio_key  = EXCLUDED.minio_key
                """,
                dialog_id, username, account_id, family,
                fam["chat_id"], int(sent.id), minio_key,
            )

        await bus.publish(
            module=Module.REPORT_DELIVERY,
            type=EventType.REPORT_DELIVERED,
            status=Status.SUCCESS,
            account_id=account_id,
            data={
                "dialog_id": dialog_id, "worker": worker_name,
                "chat_id": fam["chat_id"], "minio_key": minio_key,
                "telegram_message_id": int(sent.id),
                "awaiting_moderation": True,
            },
        )
        log.info(
            "report_bot: отчёт по диалогу %s отправлен в %s (msg=%s), "
            "ждём модерации", dialog_id, family, sent.id,
        )

    def _buttons(self, dialog_id: int, *, chosen: str | None) -> list[Any]:
        """Три кнопки; у выбранной — галочка.

        callback_data: `m:<choice>:<dialog_id>` — под лимитом 64 байта
        с огромным запасом.
        """
        row = []
        for key, label in CHOICES.items():
            text = f"✅ {label}" if key == chosen else label
            row.append(Button.inline(text, f"m:{key}:{dialog_id}".encode()))
        return [row]

    # ─── Нажатие кнопки ──────────────────────────────────────────────

    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        """Записать выбор и сдвинуть таймер. Нажимать может любой.

        Ошибки глушим и отвечаем всплывашкой: необработанное исключение
        в обработчике оставит кнопку «в часиках» у нажавшего.
        """
        try:
            raw = (event.data or b"").decode("utf-8", "replace")
            parts = raw.split(":")
            if len(parts) != 3 or parts[0] != "m":
                await event.answer("Неизвестная кнопка", alert=True)
                return
            _, choice, dialog_raw = parts
            if choice not in CHOICES:
                await event.answer("Неизвестный выбор", alert=True)
                return
            dialog_id = int(dialog_raw)
        except Exception:
            log.exception("report_bot: битый callback_data")
            try:
                await event.answer("Ошибка обработки", alert=True)
            except Exception:
                pass
            return

        pool = db.get_pool()
        try:
            sender = await event.get_sender()
            sender_name = (
                getattr(sender, "username", None)
                or " ".join(
                    p for p in (
                        getattr(sender, "first_name", "") or "",
                        getattr(sender, "last_name", "") or "",
                    ) if p
                ).strip()
                or str(event.sender_id)
            )
        except Exception:
            sender_name = str(event.sender_id)

        push_after = _now() + timedelta(seconds=MODERATION_DELAY_SEC)

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE report_moderation
                SET choice         = $2,
                    chosen_by      = $3,
                    chosen_by_name = $4,
                    chosen_at      = NOW(),
                    push_after     = $5,
                    push_attempts  = 0,
                    push_error     = NULL
                WHERE dialog_id = $1
                  AND pushed_at IS NULL
                RETURNING dialog_id
                """,
                dialog_id, choice, int(event.sender_id),
                sender_name, push_after,
            )

        if row is None:
            # Либо строки нет, либо уже отправлено в CRM — менять поздно.
            await event.answer(
                "Статус уже отправлен в CRM, изменить нельзя", alert=True,
            )
            return

        try:
            await event.edit(buttons=self._buttons(dialog_id, chosen=choice))
        except Exception:
            # Кнопки не перерисовались (сообщение удалили, FloodWait) —
            # выбор в БД уже записан, тикер отработает.
            log.warning(
                "report_bot: не удалось обновить кнопки диалога %s", dialog_id,
                exc_info=True,
            )

        hours = MODERATION_DELAY_SEC // 3600
        await event.answer(
            f"{CHOICES[choice]} — уйдёт в CRM через {hours} ч. "
            f"Можно передумать, отсчёт начнётся заново.",
        )
        log.info(
            "report_bot: диалог %s → %s (нажал %s), пуш в %s",
            dialog_id, choice, sender_name, push_after.isoformat(),
        )

    # ─── Тикер: добить просроченные ──────────────────────────────────

    async def _ticker_loop(self) -> None:
        log.info("report_bot: тикер запущен (интервал %dс)", TICKER_INTERVAL_SEC)
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=TICKER_INTERVAL_SEC,
                )
                return  # stop()
            except asyncio.TimeoutError:
                pass
            try:
                await self._push_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("report_bot: ошибка тикера")

    async def _push_due(self) -> None:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            due = await conn.fetch(
                """
                SELECT dialog_id, username, account_id, family,
                       chat_id, message_id, choice, chosen_by_name,
                       push_attempts
                FROM report_moderation
                WHERE pushed_at IS NULL
                  AND push_after IS NOT NULL
                  AND push_after <= NOW()
                  AND push_attempts < $1
                ORDER BY push_after
                LIMIT 50
                """,
                MAX_PUSH_ATTEMPTS,
            )
        if not due:
            return

        await self._load_families()
        for r in due:
            try:
                await self._finalize_one(dict(r))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "report_bot: финализация диалога %s не удалась",
                    r["dialog_id"],
                )

    async def _finalize_one(self, r: dict[str, Any]) -> None:
        dialog_id = int(r["dialog_id"])
        choice = r["choice"]
        fam = self._families.get((r["family"] or "").upper()) or {}
        l2_login = fam.get("l2_login")

        try:
            pushed_status = await self._crm.push_lead_moderated(
                account_id=r["account_id"],
                username=r["username"],
                choice=choice,
                l2_login=l2_login,
            )
        except PartnerCrmError as e:
            # CRM недоступна/ответила ошибкой. Строку не закрываем, но
            # следующую попытку отодвигаем — и после MAX_PUSH_ATTEMPTS
            # перестаём брать её в выборку совсем (см. _push_due).
            attempts = int(r.get("push_attempts") or 0) + 1
            idx = min(attempts - 1, len(_PUSH_BACKOFF_SEC) - 1)
            next_at = _now() + timedelta(seconds=_PUSH_BACKOFF_SEC[idx])
            gave_up = attempts >= MAX_PUSH_ATTEMPTS

            pool = db.get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE report_moderation
                    SET push_error    = $2,
                        push_attempts = $3,
                        push_after    = $4
                    WHERE dialog_id = $1
                    """,
                    dialog_id, str(e)[:300], attempts, next_at,
                )

            if gave_up:
                log.error(
                    "report_bot: пуш диалога %s не прошёл %d раз (%s) — "
                    "сдаёмся, нужен оператор", dialog_id, attempts, e,
                )
                await self._warn_push_failed(r, str(e), attempts)
            else:
                log.warning(
                    "report_bot: пуш диалога %s не прошёл (%s), попытка %d/%d, "
                    "следующая через %dс",
                    dialog_id, e, attempts, MAX_PUSH_ATTEMPTS,
                    _PUSH_BACKOFF_SEC[idx],
                )
            return

        pool = db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE report_moderation
                SET pushed_at     = NOW(),
                    pushed_status = $2,
                    push_error    = NULL,
                    push_attempts = 0
                WHERE dialog_id = $1
                """,
                dialog_id, pushed_status,
            )

        # Пузырь → терминальный. Ставим ПОСЛЕ отметки pushed_at:
        # finalizer увидит её и не отправит статус повторно.
        await self._set_final_bubble(dialog_id, choice)

        # Кнопки снять, итог дописать.
        await self._seal_message(r, pushed_status)

        log.info(
            "report_bot: диалог %s финализирован (%s → CRM %s)",
            dialog_id, choice, pushed_status,
        )

    async def _warn_push_failed(
        self, r: dict[str, Any], err: str, attempts: int,
    ) -> None:
        """Сказать в группу, что статус так и не ушёл в CRM.

        Кнопки НЕ снимаем и пузырь не трогаем: диалог остаётся в
        «Модерации», нажатие обнулит счётчик и запустит цикл заново.
        Отвечаем на исходное сообщение, чтобы предупреждение не потерялось.
        """
        if self._client is None:
            return
        choice_label = CHOICES.get(r.get("choice") or "", "?")
        text = (
            f"⚠️ Не удалось передать в CRM статус «{choice_label}» "
            f"после {attempts} попыток.\n"
            f"Ошибка: {err[:200]}\n"
            f"Диалог остаётся в модерации — нажмите кнопку ещё раз, "
            f"чтобы повторить."
        )
        try:
            await self._client.send_message(
                int(r["chat_id"]), text, reply_to=int(r["message_id"]),
            )
        except Exception:
            log.warning(
                "report_bot: не удалось отправить предупреждение по диалогу %s",
                r.get("dialog_id"), exc_info=True,
            )

    async def _seal_message(self, r: dict[str, Any], pushed_status: str) -> None:
        """Убрать кнопки и дописать итог в подпись."""
        if self._client is None:
            return
        choice = r["choice"]
        who = r.get("chosen_by_name")
        tail = f"\n\n📤 Передано в CRM со статусом: {CHOICES.get(choice, choice)}"
        if who:
            tail += f"\nРешение: {who}"

        try:
            msg = await self._client.get_messages(
                int(r["chat_id"]), ids=int(r["message_id"]),
            )
            base = (getattr(msg, "message", "") or "") if msg else ""
            # Подпись Telegram ограничена 1024 символами — режем начало,
            # хвост с итогом важнее.
            room = _CAPTION_MAX - len(tail)
            if len(base) > room:
                base = base[: max(0, room - 1)].rstrip() + "…"
            await self._client.edit_message(
                int(r["chat_id"]), int(r["message_id"]),
                base + tail, buttons=None,
            )
        except Exception:
            log.warning(
                "report_bot: не удалось запечатать сообщение диалога %s",
                r["dialog_id"], exc_info=True,
            )

    async def _set_final_bubble(self, dialog_id: int, choice: str) -> None:
        label = _BUBBLE_BY_CHOICE.get(choice)
        if not label:
            return
        pool = db.get_pool()
        async with pool.acquire() as conn:
            status_id = await conn.fetchval(
                "SELECT id FROM dialog_statuses WHERE label = $1", label,
            )
        if status_id is None:
            log.warning("report_bot: пузырь %r не найден в каталоге", label)
            return
        try:
            await dialog_statuses_service.set_dialog_status(
                dialog_id, int(status_id), source="moderation",
            )
        except Exception:
            log.warning(
                "report_bot: не удалось поставить пузырь %r диалогу %s",
                label, dialog_id, exc_info=True,
            )

    # ─── Данные ──────────────────────────────────────────────────────

    async def _dialog_info(
        self, dialog_id: int, account_id: int,
    ) -> dict[str, Any] | None:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT d.username AS username, a.name AS worker_name
                FROM dialogs d
                JOIN accounts a ON a.id = $2
                WHERE d.id = $1
                """,
                dialog_id, account_id,
            )
        if row is None:
            log.warning(
                "report_bot: диалог %s / аккаунт %s не найдены",
                dialog_id, account_id,
            )
            return None
        return {"username": row["username"], "worker_name": row["worker_name"]}

    async def _build_caption(self, dialog_id: int, worker_name: str) -> str:
        """Подпись к отчёту: воркер, собеседник, поля оценщика, время.

        Формат сохранён из `report_delivery` — операторы к нему привыкли.
        """
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT d.username, d.first_name, d.last_name, d.telegram_user_id,
                       e.extracted_name, e.extracted_age, e.extracted_city,
                       e.extracted_workplace, e.extracted_interests, e.evaluated_at
                FROM dialogs d
                LEFT JOIN lead_evaluations e ON e.dialog_id = d.id
                WHERE d.id = $1
                """,
                dialog_id,
            )
        ctx = dict(row) if row is not None else {}

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

        rendered = [
            f"{label}: {str(value).strip()}"
            for label, value in (
                ("Имя", ctx.get("extracted_name")),
                ("Возраст", ctx.get("extracted_age")),
                ("Город", ctx.get("extracted_city")),
                ("Работа", ctx.get("extracted_workplace")),
                ("Интересы", ctx.get("extracted_interests")),
            )
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
