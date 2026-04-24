import { useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";

// Модалка запуска нового авто-диалога. Обёртка над POST /autochat/start.
// После успеха backend резолвит @username, генерирует первое сообщение
// по initial_prompt (Opus 4.7 через OpenRouter), отправляет его и дальше
// ведёт переписку сам. См. docs/autochat.md.

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
  const [initialPrompt, setInitialPrompt] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");

  function reset() {
    setBusy(false);
    setUsername("");
    setInitialPrompt("");
    setSystemPrompt("");
  }

  function handleClose() {
    reset();
    onClose();
  }

  async function submit() {
    if (accountId == null) return;
    setBusy(true);
    try {
      // Снимаем `@` если пользователь ввёл с ним — backend ожидает чистый username.
      const cleanUsername = username.trim().replace(/^@/, "");
      const res = await api.autochatStart({
        account_id: accountId,
        username: cleanUsername,
        initial_prompt: initialPrompt,
        system_prompt: systemPrompt,
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

  const valid = username.trim().length > 0 && initialPrompt.trim().length > 0 && accountId != null;

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) handleClose(); }}>
      <DialogContent className="max-w-lg">
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
                className="mono"
                autoFocus
              />
            </div>

            <div className="flex flex-col gap-1">
              <Label htmlFor="ac-initial">Initial prompt · что LLM напишет первым</Label>
              <textarea
                id="ac-initial"
                placeholder="Напиши дружелюбное первое сообщение на тему …"
                value={initialPrompt}
                onChange={(e) => setInitialPrompt(e.target.value)}
                rows={4}
                className="flex w-full rounded-md bg-background px-2 py-1 text-sm hairline border-border placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              />
            </div>

            <div className="flex flex-col gap-1">
              <Label htmlFor="ac-system">System prompt · персонаж (опционально)</Label>
              <textarea
                id="ac-system"
                placeholder="Если пусто — берётся текст из prompts/autochat_reply_system.md"
                value={systemPrompt}
                onChange={(e) => setSystemPrompt(e.target.value)}
                rows={3}
                className="flex w-full rounded-md bg-background px-2 py-1 text-sm hairline border-border placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              />
              <div className="text-[10px] text-muted-foreground">
                Можно оставить пустым — тогда `{"{user_system_prompt}"}` подставится пустой
                строкой и правила поведения возьмутся целиком из файла.
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
