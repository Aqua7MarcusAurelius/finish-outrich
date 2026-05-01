import { useEffect, useState } from "react";
import { Copy, RefreshCw } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { api, describeApiError } from "@/lib/api";
import { useDialogs } from "@/hooks/useAccounts";
import { useToast } from "@/lib/toast";

// Превью что уйдёт в LLM при текущих (несохранённых) шаблонах. LLM
// НЕ вызывается. Источник партнёра/истории — выбираемый диалог
// этого воркера. Без диалога partner_* пустые, conversation_history
// получает placeholder-маркер.

type Props = {
  open: boolean;
  accountId: number;
  initialTemplate: string;
  replyTemplate: string;
  onClose: () => void;
};

export function PromptPreviewDialog({
  open, accountId, initialTemplate, replyTemplate, onClose,
}: Props) {
  const toast = useToast();
  const dialogsQ = useDialogs(open ? accountId : null);

  const [dialogId, setDialogId] = useState<number | null>(null);
  const [dialogPicked, setDialogPicked] = useState(false);
  const [initialText, setInitialText] = useState("");
  const [replyText, setReplyText] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function fetchPreview() {
    setLoading(true);
    setError(null);
    try {
      const r = await api.previewWorkerPrompts(accountId, {
        initial_template: initialTemplate,
        reply_template: replyTemplate,
        dialog_id: dialogId,
      });
      setInitialText(r.initial);
      setReplyText(r.reply);
    } catch (e) {
      const d = describeApiError(e);
      setError(`${d.title}${d.detail ? ` (${d.detail})` : ""}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!open) {
      setDialogId(null);
      setDialogPicked(false);
    }
  }, [open]);

  // Auto-pick случайного диалога при первом открытии (если есть из чего).
  useEffect(() => {
    if (!open || dialogPicked) return;
    const items = dialogsQ.data?.items;
    if (items && items.length > 0) {
      const random = items[Math.floor(Math.random() * items.length)];
      setDialogId(random.id);
      setDialogPicked(true);
    }
  }, [open, dialogPicked, dialogsQ.data]);

  useEffect(() => {
    if (!open) return;
    fetchPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, dialogId]);

  function selectDialog(value: number | null) {
    setDialogId(value);
    setDialogPicked(true);
  }

  async function copyAll() {
    const text =
      `=== INITIAL (system) ===\n${initialText || "(пусто)"}\n\n` +
      `=== REPLY (system) ===\n${replyText || "(пусто)"}\n`;
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
              Источник истории / собеседника
            </Label>
            <select
              id="preview-dialog"
              value={dialogId == null ? "" : String(dialogId)}
              onChange={(e) => selectDialog(e.target.value === "" ? null : Number(e.target.value))}
              className="mt-1 h-8 w-full rounded-md bg-background px-2 text-sm hairline border-border focus:outline-none focus:ring-1 focus:ring-ring"
            >
              <option value="">(без диалога — partner_* пустые, history с маркером)</option>
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
          <Button size="sm" variant="outline" onClick={copyAll} disabled={!initialText && !replyText}>
            <Copy className="h-3 w-3" /> Скопировать
          </Button>
        </div>

        {error && <div className="text-xs text-destructive">{error}</div>}

        <div className="mt-2 flex flex-col gap-3">
          <PreviewBlock label="initial · system" text={initialText} loading={loading} />
          <PreviewBlock label="reply · system"   text={replyText}   loading={loading} />
        </div>

        <div className="flex justify-end pt-1">
          <Button size="sm" variant="ghost" onClick={onClose}>Закрыть</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function PreviewBlock({ label, text, loading }: { label: string; text: string; loading: boolean }) {
  return (
    <div>
      <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <pre className="mono max-h-[40vh] overflow-auto whitespace-pre-wrap rounded-md bg-muted/40 p-3 text-[11px] leading-relaxed hairline border-border">
        {loading ? "загрузка…" : text || "(пусто)"}
      </pre>
    </div>
  );
}
