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
