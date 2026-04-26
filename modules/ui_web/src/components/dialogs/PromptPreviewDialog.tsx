import { useEffect, useState } from "react";
import { Copy, RefreshCw } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { api, describeApiError } from "@/lib/api";
import { useDialogs } from "@/hooks/useAccounts";
import { useToast } from "@/lib/toast";

// Превью того, что уйдёт в chat_completion при текущих (несохранённых)
// значениях полей редактора. LLM НЕ вызывается.
//
// Источник истории — выбираемый диалог этого воркера. Без диалога —
// placeholder вместо {conversation_history}, удобно посмотреть структуру
// промта без шума длинной истории.

type FormValues = {
  fabula: string;
  bio: string;
  style: string;
  forbidden: string;
  length_hint: string;
  goals: string;
  format_reply: string;
  examples: string;
};

type Props = {
  open: boolean;
  accountId: number;
  form: FormValues;
  onClose: () => void;
};

export function PromptPreviewDialog({ open, accountId, form, onClose }: Props) {
  const toast = useToast();
  const dialogsQ = useDialogs(open ? accountId : null);

  const [dialogId, setDialogId] = useState<number | null>(null);
  // true как только оператор сам выбрал что-то из dropdown (или auto-pick
  // отработал) — защищает от повторного авто-выбора при рефетче списка.
  const [dialogPicked, setDialogPicked] = useState(false);
  const [systemText, setSystemText] = useState("");
  const [userText, setUserText] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function fetchPreview() {
    setLoading(true);
    setError(null);
    try {
      const r = await api.previewWorkerPrompts(accountId, {
        ...form,
        dialog_id: dialogId,
      });
      setSystemText(r.system);
      setUserText(r.user);
    } catch (e) {
      const d = describeApiError(e);
      setError(`${d.title}${d.detail ? ` (${d.detail})` : ""}`);
    } finally {
      setLoading(false);
    }
  }

  // Сброс выбора при закрытии — следующее открытие снова авто-выберет
  // случайный диалог.
  useEffect(() => {
    if (!open) {
      setDialogId(null);
      setDialogPicked(false);
    }
  }, [open]);

  // Авто-выбор случайного диалога при первом открытии (если есть из чего).
  // Срабатывает один раз — флаг dialogPicked блокирует повторный авто-pick.
  useEffect(() => {
    if (!open || dialogPicked) return;
    const items = dialogsQ.data?.items;
    if (items && items.length > 0) {
      const random = items[Math.floor(Math.random() * items.length)];
      setDialogId(random.id);
      setDialogPicked(true);
    }
  }, [open, dialogPicked, dialogsQ.data]);

  // Собираем превью при открытии и при смене диалога. Перенабор формы —
  // оператор жмёт "Обновить" сам (не дёргаем бэк на каждый keystroke).
  useEffect(() => {
    if (!open) return;
    fetchPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, dialogId]);

  function selectDialog(value: number | null) {
    setDialogId(value);
    setDialogPicked(true);
  }

  async function copy() {
    const text = `=== SYSTEM ===\n${systemText}\n\n=== USER ===\n${userText}\n`;
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Скопировано в буфер");
    } catch {
      toast.error("Не удалось скопировать", "браузер заблокировал доступ к буферу");
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent className="max-w-5xl">
        <DialogHeader>
          <DialogTitle>Что уйдёт в LLM</DialogTitle>
        </DialogHeader>

        <div className="flex items-end gap-3">
          <div className="flex-1">
            <Label htmlFor="preview-dialog" className="text-xs">
              Источник истории
            </Label>
            <select
              id="preview-dialog"
              value={dialogId == null ? "" : String(dialogId)}
              onChange={(e) => selectDialog(e.target.value === "" ? null : Number(e.target.value))}
              className="mt-1 h-8 w-full rounded-md bg-background px-2 text-sm hairline border-border focus:outline-none focus:ring-1 focus:ring-ring"
            >
              <option value="">(без истории — только структура промта)</option>
              {(dialogsQ.data?.items ?? []).map((d) => (
                <option key={d.id} value={d.id}>
                  {d.title}{d.username ? ` · @${d.username}` : ""} · #{d.id}
                </option>
              ))}
            </select>
          </div>
          <Button size="sm" variant="outline" onClick={fetchPreview} disabled={loading}>
            <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} /> Обновить
          </Button>
          <Button size="sm" variant="outline" onClick={copy} disabled={!systemText}>
            <Copy className="h-3 w-3" /> Скопировать
          </Button>
        </div>

        {error && <div className="text-xs text-destructive">{error}</div>}

        <div className="mt-2 flex flex-col gap-3">
          <div>
            <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
              system
            </div>
            <pre className="mono max-h-[55vh] overflow-auto whitespace-pre-wrap rounded-md bg-muted/40 p-3 text-[11px] leading-relaxed hairline border-border">
              {loading ? "загрузка…" : systemText || "(пусто)"}
            </pre>
          </div>
          <div>
            <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
              user
            </div>
            <pre className="mono whitespace-pre-wrap rounded-md bg-muted/40 p-2 text-[11px] hairline border-border">
              {userText || "(пусто)"}
            </pre>
          </div>
        </div>

        <div className="flex justify-end pt-1">
          <Button size="sm" variant="ghost" onClick={onClose}>Закрыть</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
