import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useSseConnected } from "@/hooks/useLiveInvalidator";

// L5 (2026-07-13): когда SSE открыт — push-обновления приходят мгновенно,
// поллинг нужен лишь как редкий safety-net (60с). Когда SSE НЕ в состоянии
// OPEN (обрыв/реконнект) — push молчит, поэтому поллим часто (12с), чтобы UI
// не «замирал» на время реконнекта EventSource.
const SLOW_POLL_MS = 60_000;
const FAST_POLL_MS = 12_000;

export function useAccounts() {
  const sseOk = useSseConnected();
  return useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.listAccounts(),
    refetchInterval: sseOk ? SLOW_POLL_MS : FAST_POLL_MS,
  });
}

export function useDialogs(accountId: number | null) {
  return useQuery({
    queryKey: ["dialogs", accountId],
    queryFn: () => api.listDialogs(accountId!),
    enabled: accountId != null,
  });
}

export function useMessages(dialogId: number | null) {
  return useQuery({
    queryKey: ["messages", dialogId],
    queryFn: () => api.listMessages(dialogId!),
    enabled: dialogId != null,
  });
}

export function useDialogProfile(dialogId: number | null) {
  return useQuery({
    queryKey: ["dialog", dialogId],
    queryFn: () => api.getDialog(dialogId!),
    enabled: dialogId != null,
  });
}

// Каталог операторских пузырей. Используется и страницей настроек
// (CRUD), и DialogListItem (рендер dropdown). Меняется редко —
// staleTime 60s достаточно. Инвалидация после mutate — вручную
// через queryClient.invalidateQueries(["dialog-statuses"]).
export function useDialogStatuses() {
  return useQuery({
    queryKey: ["dialog-statuses"],
    queryFn: () => api.listDialogStatuses().then((r) => r.items),
    staleTime: 60_000,
  });
}

// Состояние авторежима для воркера. Обновляется через SSE (события
// auto_runner.*, worker.stopped итд — см. useLiveInvalidator). Polling
// оставлен как safety-net на случай если SSE отвалится.
export function useAutoRunner(accountId: number | null) {
  const sseOk = useSseConnected();
  return useQuery({
    queryKey: ["auto-runner", accountId],
    queryFn: () => api.getAutoRunner(accountId!),
    enabled: accountId != null,
    // L5: медленный поллинг при живом SSE, быстрый — когда push молчит.
    refetchInterval: sseOk ? SLOW_POLL_MS : FAST_POLL_MS,
  });
}
