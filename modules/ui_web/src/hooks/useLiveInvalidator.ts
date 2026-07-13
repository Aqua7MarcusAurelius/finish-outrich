import { useEffect, useSyncExternalStore } from "react";
import { useQueryClient, type QueryKey } from "@tanstack/react-query";
import { openEventStream } from "@/lib/api";
import type { BusEvent } from "@/types/api";

// ─── L5 (2026-07-13): статус SSE-соединения ──────────────────────────
// Общий внешний стор: OPEN → push-обновления идут, поллинг медленный (60с);
// не-OPEN (реконнект/обрыв) → быстрый safety-поллинг (см. useAccounts).
let sseConnected = false;
const sseListeners = new Set<() => void>();

function setSseConnected(v: boolean): void {
  if (sseConnected === v) return;
  sseConnected = v;
  sseListeners.forEach((l) => l());
}

/** Реактивный флаг: открыт ли SSE-канал прямо сейчас. */
export function useSseConnected(): boolean {
  return useSyncExternalStore(
    (cb) => {
      sseListeners.add(cb);
      return () => sseListeners.delete(cb);
    },
    () => sseConnected,
    () => false,
  );
}

/**
 * Глобальный SSE-подписчик. Монтируется один раз в корне App'а.
 *
 * Открывает единственный EventSource на /events/stream БЕЗ фильтров и на
 * каждое релевантное событие инвалидирует соответствующий React-Query
 * ключ. Это заменяет `refetchInterval` в отдельных хуках: UI обновляется
 * push-моделью через Redis Stream, полить каждые N секунд больше не нужно.
 *
 * EventSource встроенно реконнектится на разрыв (browser standard) — при
 * этом бэк корректно обрабатывает Last-Event-ID (см. api/routes/events.py).
 *
 * Что *не* инвалидируем:
 *   - `["event-*"]` — страница /events сама подписана на SSE отдельно
 *     через useEventStream() c фильтрами. Дублировать не надо.
 *   - `["dialog-events", ...]` — DialogEventsWidget сам решает когда
 *     обновляться (пока что через staleTime; можно отдельно подписать).
 */

type QueryKeys = ReadonlyArray<QueryKey>;

const RULES: Record<string, (e: BusEvent) => QueryKeys> = {
  // ─── Сообщения ───────────────────────────────────────────────────
  // MESSAGE_RECEIVED — ключевое: живой чат должен обновляться мгновенно.
  "message.received": (e) => {
    const dialogId = (e.data as { dialog_id?: number })?.dialog_id;
    const keys: QueryKey[] = [["accounts"]];
    if (dialogId != null) {
      keys.push(["messages", dialogId]);
      keys.push(["dialog", dialogId]);
    }
    if (e.account_id != null) keys.push(["dialogs", e.account_id]);
    return keys;
  },
  "message.saved": (e) => {
    const dialogId = (e.data as { dialog_id?: number })?.dialog_id;
    const keys: QueryKey[] = [];
    if (dialogId != null) keys.push(["messages", dialogId]);
    if (e.account_id != null) keys.push(["dialogs", e.account_id]);
    return keys;
  },
  "message.updated": (e) => {
    const dialogId = (e.data as { dialog_id?: number })?.dialog_id;
    return dialogId != null ? [["messages", dialogId]] : [];
  },

  // ─── Диалоги ─────────────────────────────────────────────────────
  "dialog.created": (e) =>
    e.account_id != null ? [["dialogs", e.account_id], ["accounts"]] : [["accounts"]],
  "dialog.deleted": (e) => {
    const keys: QueryKey[] = [["accounts"]];
    if (e.account_id != null) keys.push(["dialogs", e.account_id]);
    return keys;
  },
  "dialog.profile.fetched": (e) => {
    const dialogId = (e.data as { dialog_id?: number })?.dialog_id;
    return dialogId != null ? [["dialog", dialogId]] : [];
  },
  "dialog.status_changed": (e) => {
    const dialogId = (e.data as { dialog_id?: number })?.dialog_id;
    const keys: QueryKey[] = [];
    if (dialogId != null) keys.push(["dialog", dialogId]);
    if (e.account_id != null) keys.push(["dialogs", e.account_id]);
    return keys;
  },
  "dialog_statuses.catalog_changed": () => [["dialog-statuses"]],
  "account.dialogs_wiped": (e) => {
    const keys: QueryKey[] = [["accounts"]];
    if (e.account_id != null) keys.push(["dialogs", e.account_id]);
    return keys;
  },

  // ─── Autochat (страница диалога, панель авточата) ────────────────
  "autochat.started": (e) => {
    const dialogId = (e.data as { dialog_id?: number })?.dialog_id;
    const keys: QueryKey[] = [];
    if (dialogId != null) keys.push(["dialog-autochat", dialogId]);
    if (e.account_id != null) keys.push(["dialogs", e.account_id]);
    return keys;
  },
  "autochat.session_stopped": (e) => {
    const dialogId = (e.data as { dialog_id?: number })?.dialog_id;
    const keys: QueryKey[] = [];
    if (dialogId != null) keys.push(["dialog-autochat", dialogId]);
    if (e.account_id != null) keys.push(["dialogs", e.account_id]);
    return keys;
  },
  "autochat.session_error": (e) => {
    const dialogId = (e.data as { dialog_id?: number })?.dialog_id;
    return dialogId != null ? [["dialog-autochat", dialogId]] : [];
  },
  "autochat.report_saved": (e) => {
    const dialogId = (e.data as { dialog_id?: number })?.dialog_id;
    return dialogId != null ? [["dialog-autochat", dialogId]] : [];
  },
  "autochat.finished_by_llm": (e) => {
    const dialogId = (e.data as { dialog_id?: number })?.dialog_id;
    return dialogId != null ? [["dialog-autochat", dialogId]] : [];
  },

  // ─── Воркеры и аккаунты ──────────────────────────────────────────
  "worker.started": (e) => {
    const keys: QueryKey[] = [["accounts"]];
    if (e.account_id != null) keys.push(["auto-runner", e.account_id]);
    return keys;
  },
  "worker.stopped": (e) => {
    const keys: QueryKey[] = [["accounts"]];
    if (e.account_id != null) keys.push(["auto-runner", e.account_id]);
    return keys;
  },
  "worker.restarted": () => [["accounts"]],
  "worker.crashed": () => [["accounts"]],
  "account.created": () => [["accounts"]],
  "account.reauthorized": () => [["accounts"]],
  "account.session_expired": () => [["accounts"]],
  "account.deleted": () => [["accounts"]],

  // ─── Auto-runner ─────────────────────────────────────────────────
  "auto_runner.started": (e) =>
    e.account_id != null ? [["auto-runner", e.account_id], ["accounts"]] : [["accounts"]],
  "auto_runner.stopped": (e) =>
    e.account_id != null ? [["auto-runner", e.account_id], ["accounts"]] : [["accounts"]],
  "auto_runner.lead_sent": (e) =>
    e.account_id != null ? [["auto-runner", e.account_id]] : [],
  "auto_runner.lead_skipped": (e) =>
    e.account_id != null ? [["auto-runner", e.account_id]] : [],
  "auto_runner.queue_empty": (e) =>
    e.account_id != null ? [["auto-runner", e.account_id]] : [],

  // ─── Lead evaluator ──────────────────────────────────────────────
  "lead_evaluator.completed": (e) => {
    const dialogId = (e.data as { dialog_id?: number })?.dialog_id;
    return dialogId != null
      ? [["lead-evaluation", dialogId], ["dialog", dialogId]]
      : [];
  },

  // ─── Partner CRM ─────────────────────────────────────────────────
  // pool_refilled/lead_taken/status_pushed отражаются на панели
  // аккаунта (счётчики). Мягко трогаем ["accounts"].
  "partner_crm.pool_refilled": () => [["accounts"]],
  "partner_crm.lead_taken": () => [["accounts"]],
  "partner_crm.status_pushed": () => [["accounts"]],
};

export function useLiveInvalidator(): void {
  const qc = useQueryClient();

  useEffect(() => {
    const src = openEventStream(
      {},
      (event) => {
        const rule = RULES[event.type];
        if (!rule) return;
        const keys = rule(event);
        for (const key of keys) {
          qc.invalidateQueries({ queryKey: key });
        }
      },
      () => {
        // L5: обрыв/реконнект — помечаем канал закрытым, чтобы хуки
        // перешли на быстрый safety-поллинг. EventSource сам реконнектится
        // и по "open" мы вернём true.
        setSseConnected(false);
      },
    );
    src.addEventListener("open", () => setSseConnected(true));
    return () => {
      setSseConnected(false);
      src.close();
    };
  }, [qc]);
}
