import { useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";

// Модалка запуска нового авто-диалога. Обёртка над POST /autochat/start.
//
// Per-session промтов / задач больше нет — всё что нужно для генерации
// первого сообщения и ведения переписки берётся из per-worker конфига
// (account_prompts, страница /workers/{id}/prompt). Здесь только адресат.

type Props = {
  open: boolean;
  accountId: number | null;
  onClose: () => void;
  onCreated: (sessionId: number) => void;
};

export function NewDialogDialog({ open, accountId, onClose, onCreated }: Props) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("");

  function reset() {
    setBusy(false);
    setUsername("");
  }

  function handleClose() {
    reset();
    onClose();
  }

  async function submit() {
    if (accountId == null) return;
    setBusy(true);
    try {
      const cleanUsername = username.trim().replace(/^@/, "");
      const res = await api.autochatStart({
        account_id: accountId,
        username: cleanUsername,
      });
      toast.success(
        `Диалог запущен: @${cleanUsername}`,
        `session_id=${res.session.id} · ${res.session.status}`,
      );
      reset();
      onCreated(res.session.id);
      onClose();
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`AutoChat start: ${d.title}`, d.detail);
    } finally {
      setBusy(false);
    }
  }

  const valid = username.trim().length > 0 && accountId != null;

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) handleClose(); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>
            Новый авто-диалог
            {accountId != null && (
              <span className="ml-2 mono text-[10px] text-muted-foreground">
                account_{accountId}
              </span>
            )}
          </DialogTitle>
        </DialogHeader>

        {accountId == null ? (
          <div className="text-xs text-destructive">
            Сначала выбери аккаунт в шапке — от него пойдёт сообщение.
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <Label htmlFor="ac-username">Кому · @username</Label>
              <Input
                id="ac-username"
                placeholder="durov"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && valid && !busy) submit(); }}
                className="mono"
                autoFocus
              />
              <div className="text-[10px] text-muted-foreground">
                Текст первого сообщения и поведение в переписке берутся из промта воркера.
              </div>
            </div>

            <div className="flex justify-end gap-2 pt-1">
              <Button variant="ghost" size="sm" onClick={handleClose} disabled={busy}>Отмена</Button>
              <Button size="sm" onClick={submit} disabled={!valid || busy}>
                {busy ? "отправляем…" : "Запустить"}
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
