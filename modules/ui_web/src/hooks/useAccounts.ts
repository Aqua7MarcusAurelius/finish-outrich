import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useAccounts() {
  return useQuery({
    queryKey: ["accounts"],
    queryFn: () => api.listAccounts(),
    refetchInterval: 5_000, // worker status is live — poll it
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

// Состояние авторежима для воркера. Поллится — потому что автомат
// может сам выключиться (например, после worker.stopped), и UI должен
// это отражать без рефреша страницы.
export function useAutoRunner(accountId: number | null) {
  return useQuery({
    queryKey: ["auto-runner", accountId],
    queryFn: () => api.getAutoRunner(accountId!),
    enabled: accountId != null,
    refetchInterval: 10_000,
  });
}
