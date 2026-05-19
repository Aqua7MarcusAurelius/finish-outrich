"""Сервис оценщика лидов.

LeadEvaluatorService — фоновый consumer шины + публичный метод
`evaluate(dialog_id)` для ручного вызова из routes.

Поток работы (один вызов `evaluate`):
1. Загрузить переписку и профиль диалога.
2. Собрать messages с системным промтом из `prompts/lead_evaluator_system.md`.
3. Позвать `openrouter.chat_completion(model=OPENROUTER_MODEL_EVALUATOR)`.
4. Распарсить ответ: `<verdict>...</verdict>` + опциональные поля.
5. Если verdict не нашёлся — один ретрай (`lead_evaluator.parse_retries`).
6. UPSERT в `lead_evaluations` (UNIQUE(dialog_id)).
7. Поставить терминальный пузырь на диалог через
   `dialog_statuses.set_dialog_status` — `success` → "Завершен успешно",
   `archive` → "Завершен провал". Также пишем `dialogs.partner_crm_terminal_reason`
   чтобы partner_crm потом знал какой код слать в CRM.
8. Опубликовать `lead_evaluator.completed` / `.parse_failed`.

Consumer loop слушает `autochat.finished_by_llm` — после этого события
автоматом зовёт `evaluate` для соответствующего диалога.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from core import bus, db
from core import openrouter
from core.config import settings
from core.events import EventType, Module, Status

from modules.dialog_statuses import service as dialog_statuses_service

from .errors import DialogNotFound, EvaluationAlreadyRunning, LlmCallFailed, PromptNotFound
from .parser import EvaluationResult, parse_evaluator_response
from .prompts import build_evaluator_messages

log = logging.getLogger(__name__)

EVALUATOR_GROUP = "lead-evaluator"
EVALUATOR_CONSUMER = "lead-evaluator-1"

DEFAULT_PARSE_RETRIES = 1


# ─── маппинг verdict → terminal_reason / label ──────────────────────────

# Локальные пузыри из миграции 0012:
_LABEL_BY_VERDICT = {
    "success": "Завершен успешно",
    "archive": "Завершен провал",
}

# В terminal_reason пишем то, что partner_crm потом отправит в CRM.
_TERMINAL_REASON_BY_VERDICT = {
    "success": "ready_for_transfer",
    # archive у оценщика = "лид не подходит". По договорённости в CRM
    # шлём no_response (см. 10.0). Если позже партнёр добавит archive
    # как отдельный статус — поменяем здесь.
    "archive": "no_response",
}


async def _get_int_setting(key: str, default: int) -> int:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT value FROM settings WHERE key=$1", key,
        )
    try:
        return int(val) if val is not None else default
    except Exception:
        return default


async def _find_status_by_label(label: str) -> dict[str, Any] | None:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, label, color, sort_order, partner_terminal_status
            FROM dialog_statuses
            WHERE label = $1
            LIMIT 1
            """,
            label,
        )
    return dict(row) if row else None


class LeadEvaluatorService:
    """
    Создаётся в lifespan после `WorkerManager`/`AutoChatService` —
    зависимостей на них нет, но логически идёт после них.

    Держит `_running_dialog_ids` для защиты от двойного запуска на один и
    тот же диалог (например, autochat.finished_by_llm + ручной запрос
    оператора одновременно).
    """

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._running: set[int] = set()
        self._running_lock = asyncio.Lock()
        # Кэш ID пузырей по лейблу — заполняется лениво.
        self._label_cache: dict[str, int] = {}

    # ─── Lifecycle ───────────────────────────────────────────────────

    async def run(self) -> None:
        """Consumer-loop. Слушает autochat.finished_by_llm."""
        await bus.ensure_group(EVALUATOR_GROUP)
        log.info("LeadEvaluatorService loop started")

        while not self._stop_event.is_set():
            try:
                batch = await bus.read_group(
                    EVALUATOR_GROUP, EVALUATOR_CONSUMER,
                    count=20, block_ms=5000,
                )
                if not batch:
                    continue
                ack_ids: list[str] = []
                for stream_id, event in batch:
                    try:
                        await self._handle(event)
                        ack_ids.append(stream_id)
                        await bus.record_success(EVALUATOR_GROUP, stream_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.exception(
                            "lead_evaluator: failed event %s", event.get("id"),
                        )
                        force_ack = await bus.record_failure(
                            EVALUATOR_GROUP, stream_id, event, e,
                        )
                        if force_ack:
                            ack_ids.append(stream_id)
                if ack_ids:
                    await bus.ack_group(EVALUATOR_GROUP, ack_ids)
            except asyncio.CancelledError:
                log.info("LeadEvaluatorService loop cancelled")
                raise
            except Exception:
                log.exception("LeadEvaluatorService loop error")
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stop_event.set()

    # ─── Bus dispatcher ──────────────────────────────────────────────

    async def _handle(self, event: dict) -> None:
        et = event.get("type")
        if et == EventType.AUTOCHAT_FINISHED_BY_LLM:
            await self._on_finished_by_llm(event)
        elif et == EventType.MESSAGE_SAVED:
            await self._on_message_saved(event)

    async def _on_finished_by_llm(self, event: dict) -> None:
        data = event.get("data") or {}
        dialog_id = data.get("dialog_id")
        if dialog_id is None:
            # AutoChat не всегда передаёт dialog_id (если сессия упала
            # до создания диалога) — без него оценщик ничего не сделает.
            return
        try:
            await self.evaluate(int(dialog_id), source="autochat.finished_by_llm")
        except EvaluationAlreadyRunning:
            log.info(
                "lead_evaluator: dialog %s already being evaluated, skip",
                dialog_id,
            )
        except Exception:
            log.exception(
                "lead_evaluator: evaluate failed for dialog %s", dialog_id,
            )

    async def _on_message_saved(self, event: dict) -> None:
        """
        Автоматические транзиции по факту сохранения сообщения:
          NULL + outgoing                → "Первый контакт"
          NULL + incoming                → "Общение" (редко — мы инициируем)
          "Первый контакт" + incoming    → "Общение" (лид ответил)
          "Обрыв" + incoming             → "Общение" (вернулся)
        Терминальные пузыри (Завершен/Невалид) не трогаем — они финальные.
        """
        data = event.get("data") or {}
        dialog_id = data.get("dialog_id")
        is_outgoing = bool(data.get("is_outgoing", False))
        if dialog_id is None:
            return

        pool = db.get_pool()
        async with pool.acquire() as conn:
            current_status_id = await conn.fetchval(
                "SELECT status_id FROM dialogs WHERE id = $1", dialog_id,
            )

        # Лукапы по лейблу — один раз кэшируем за время жизни процесса.
        s_pervyi = await self._status_id("Первый контакт")
        s_obshenie = await self._status_id("Общение")
        s_obryv = await self._status_id("Обрыв")

        target: int | None = None
        if current_status_id is None:
            target = s_pervyi if is_outgoing else s_obshenie
        elif current_status_id == s_pervyi and not is_outgoing:
            target = s_obshenie
        elif current_status_id == s_obryv and not is_outgoing:
            target = s_obshenie

        if target is None or target == current_status_id:
            return

        try:
            await dialog_statuses_service.set_dialog_status(
                int(dialog_id), target,
                source="event:message_saved.auto_transition",
            )
        except dialog_statuses_service.DialogStatusError as e:
            log.warning(
                "lead_evaluator: failed to auto-transition dialog %s: %s",
                dialog_id, e,
            )

    # ─── Кэш лукапов по лейблу ───────────────────────────────────────

    async def _status_id(self, label: str) -> int | None:
        """
        Кэш живёт всю жизнь процесса. Если оператор переименует пузырь —
        первый промах потянет апдейт. Дешевле чем SELECT на каждое
        message.saved (а их много).
        """
        cached = self._label_cache.get(label)
        if cached is not None:
            return cached
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row_id = await conn.fetchval(
                "SELECT id FROM dialog_statuses WHERE label = $1 LIMIT 1",
                label,
            )
        if row_id is not None:
            self._label_cache[label] = int(row_id)
            return int(row_id)
        return None

    # ─── Публичный API ───────────────────────────────────────────────

    async def evaluate(
        self,
        dialog_id: int,
        *,
        source: str = "manual",
    ) -> dict[str, Any]:
        """
        Прогнать оценщика по диалогу. Идемпотентный по dialog_id (UPSERT).

        Возвращает запись `lead_evaluations` в виде dict.
        """
        async with self._running_lock:
            if dialog_id in self._running:
                raise EvaluationAlreadyRunning()
            self._running.add(dialog_id)

        try:
            return await self._evaluate_inner(dialog_id, source=source)
        finally:
            async with self._running_lock:
                self._running.discard(dialog_id)

    async def _evaluate_inner(
        self, dialog_id: int, *, source: str,
    ) -> dict[str, Any]:
        pool = db.get_pool()

        async with pool.acquire() as conn:
            dialog_row = await conn.fetchrow(
                """
                SELECT id, account_id, telegram_user_id, username,
                       gender, from_crm
                FROM dialogs WHERE id = $1
                """,
                dialog_id,
            )
            if dialog_row is None:
                raise DialogNotFound(f"dialog {dialog_id} not found")

            account_id = int(dialog_row["account_id"])
            gender = dialog_row["gender"]

            # Сбор messages для LLM (использует те же хелперы что autochat).
            now = datetime.now(timezone.utc)
            messages = await build_evaluator_messages(
                conn,
                account_id=account_id,
                dialog_id=dialog_id,
                gender=gender,
                now=now,
            )

        await bus.publish(
            module=Module.LEAD_EVALUATOR,
            type=EventType.LEAD_EVALUATOR_STARTED,
            status=Status.IN_PROGRESS,
            account_id=account_id,
            data={"dialog_id": dialog_id, "source": source},
        )

        retries = await _get_int_setting(
            "lead_evaluator.parse_retries", DEFAULT_PARSE_RETRIES,
        )

        result, raw_response = await self._call_llm_with_retries(
            messages, retries=retries,
        )

        # Если парсинг сломан и после ретраев — ставим archive защитно.
        if not result.parse_succeeded:
            result.verdict = "archive"
            await bus.publish(
                module=Module.LEAD_EVALUATOR,
                type=EventType.LEAD_EVALUATOR_PARSE_FAILED,
                status=Status.ERROR,
                account_id=account_id,
                data={
                    "dialog_id": dialog_id,
                    "raw_preview": (raw_response or "")[:500],
                },
            )

        # UPSERT в lead_evaluations
        payload = result.to_db_payload()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO lead_evaluations (
                    dialog_id, verdict,
                    extracted_name, extracted_age, extracted_city,
                    extracted_workplace, extracted_interests,
                    raw_response, evaluator_model, parse_succeeded,
                    evaluated_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW(), NOW()
                )
                ON CONFLICT (dialog_id) DO UPDATE SET
                    verdict             = EXCLUDED.verdict,
                    extracted_name      = EXCLUDED.extracted_name,
                    extracted_age       = EXCLUDED.extracted_age,
                    extracted_city      = EXCLUDED.extracted_city,
                    extracted_workplace = EXCLUDED.extracted_workplace,
                    extracted_interests = EXCLUDED.extracted_interests,
                    raw_response        = EXCLUDED.raw_response,
                    evaluator_model     = EXCLUDED.evaluator_model,
                    parse_succeeded     = EXCLUDED.parse_succeeded,
                    evaluated_at        = NOW(),
                    updated_at          = NOW()
                RETURNING *
                """,
                dialog_id, payload["verdict"],
                payload["extracted_name"], payload["extracted_age"],
                payload["extracted_city"], payload["extracted_workplace"],
                payload["extracted_interests"],
                raw_response, settings.OPENROUTER_MODEL_EVALUATOR,
                payload["parse_succeeded"],
            )

            # Записать причину для будущего push в CRM (всегда — это
            # owned by lead_evaluator, не нарушает 1-writer principle).
            terminal_reason = _TERMINAL_REASON_BY_VERDICT[result.verdict or "archive"]
            await conn.execute(
                """
                UPDATE dialogs SET partner_crm_terminal_reason = $2, updated_at = NOW()
                WHERE id = $1
                """,
                dialog_id, terminal_reason,
            )

        # Проставить терминальный пузырь через dialog_statuses
        # (тот опубликует dialog.status_changed; partner_crm в 10.8
        # подхватит и пушнёт финальный статус в CRM).
        label = _LABEL_BY_VERDICT[result.verdict or "archive"]
        status_card = await _find_status_by_label(label)
        if status_card is not None:
            try:
                await dialog_statuses_service.set_dialog_status(
                    dialog_id, status_card["id"],
                    source=f"event:{source}",
                )
            except dialog_statuses_service.DialogStatusError as e:
                log.warning(
                    "lead_evaluator: failed to set status on dialog %s: %s",
                    dialog_id, e,
                )
        else:
            log.warning(
                "lead_evaluator: status with label %r not in catalog — "
                "dialog %s оценён но пузырь не выставлен",
                label, dialog_id,
            )

        await bus.publish(
            module=Module.LEAD_EVALUATOR,
            type=EventType.LEAD_EVALUATOR_COMPLETED,
            status=Status.SUCCESS,
            account_id=account_id,
            data={
                "dialog_id": dialog_id,
                "verdict": result.verdict,
                "source": source,
                "extracted": {
                    "name": result.name,
                    "age": result.age,
                    "city": result.city,
                    "workplace": result.workplace,
                    "interests": result.interests,
                },
                "parse_succeeded": result.parse_succeeded,
            },
        )

        return dict(row)

    # ─── LLM helper ──────────────────────────────────────────────────

    async def _call_llm_with_retries(
        self, messages: list[dict[str, str]], *, retries: int,
    ) -> tuple[EvaluationResult, str]:
        """
        Вызывает LLM, парсит. Если verdict не найден — ретраит.
        retries = 1 значит "одна повторная попытка после первой",
        итого до 2 запросов.
        """
        attempts = max(1, retries + 1)
        last_raw = ""
        last_result: EvaluationResult | None = None

        for attempt in range(1, attempts + 1):
            try:
                raw = await openrouter.chat_completion(
                    messages,
                    model=settings.OPENROUTER_MODEL_EVALUATOR,
                    temperature=0.4,
                    # Жёсткий потолок — оценщику нужно лишь несколько
                    # тегов плюс пара фраз рассуждения. Без явного
                    # max_tokens OpenRouter резервирует контекст Opus
                    # под максимум модели (65k), и при низком балансе
                    # запрос отбивается с HTTP 402 "недостаточно кредитов".
                    max_tokens=2000,
                )
            except openrouter.OpenRouterError as e:
                raise LlmCallFailed(f"LLM call failed: {e}") from e

            last_raw = raw or ""
            result = parse_evaluator_response(last_raw)
            last_result = result
            if result.parse_succeeded:
                return result, last_raw
            log.info(
                "lead_evaluator: parse failed on attempt %d/%d", attempt, attempts,
            )

        # Все попытки без verdict — возвращаем последний с parse_succeeded=False
        assert last_result is not None
        return last_result, last_raw

    # ─── Чтение для UI ───────────────────────────────────────────────

    async def get_evaluation(self, dialog_id: int) -> dict[str, Any] | None:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, dialog_id, verdict,
                       extracted_name, extracted_age, extracted_city,
                       extracted_workplace, extracted_interests,
                       evaluator_model, parse_succeeded,
                       evaluated_at, updated_at
                FROM lead_evaluations WHERE dialog_id = $1
                """,
                dialog_id,
            )
        return dict(row) if row else None
