import { useEffect, useMemo, useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ErrorBox } from "@/components/common/ErrorBox";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";
import { useAutoRunner } from "@/hooks/useAccounts";

// Настройки авторежима одного воркера: очередь юзернеймов (по одному
// на строку), интервал между стартами, jitter. Save кладёт PATCH'ем
// на бэк. is_running и last_status — read-only, рендерим как
// информацию в шапке.

type Props = {
  open: boolean;
  accountId: number | null;
  accountLabel: string;
  onClose: () => void;
};

const MIN_INTERVAL = 60;     // дублируем MIN_INTERVAL_SEC из бэка
const MAX_QUEUE = 1000;      // дублируем MAX_QUEUE_LEN из бэка

function parseQueueText(text: string): string[] {
  return text
    .split(/[\r\n]+/)
    .map((s) => s.trim().replace(/^@/, ""))
    .filter(Boolean);
}

function formatSeconds(sec: number): string {
  if (sec < 60) return `${sec} сек`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return s === 0 ? `${m} мин` : `${m} мин ${s} сек`;
}

export function AutoRunnerSettingsDialog({ open, accountId, accountLabel, onClose }: Props) {
  const toast = useToast();
  const q = useAutoRunner(open ? accountId : null);

  // Локальные draft'ы — наполняются при загрузке, синхронизируются при
  // смене аккаунта. Save сравнивает с актуальным состоянием.
  const [queueText, setQueueText] = useState("");
  const [intervalSec, setIntervalSec] = useState<number>(1200);
  const [jitterSec, setJitterSec] = useState<number>(300);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    const d = q.data;
    if (!d) return;
    setQueueText(d.queue.join("\n"));
    setIntervalSec(d.interval_sec);
    setJitterSec(d.jitter_sec);
  }, [open, q.data]);

  const parsedQueue = useMemo(() => parseQueueText(queueText), [queueText]);
  const intervalValid = intervalSec >= MIN_INTERVAL;
  const jitterValid = jitterSec >= 0;
  const queueValid = parsedQueue.length <= MAX_QUEUE;
  const formValid = intervalValid && jitterValid && queueValid;

  const minDelay = Math.max(MIN_INTERVAL, intervalSec - jitterSec);
  const maxDelay = Math.max(MIN_INTERVAL, intervalSec + jitterSec);

  async function save() {
    if (accountId == null || !formValid) return;
    setSaving(true);
    try {
      await api.patchAutoRunner(accountId, {
        queue: parsedQueue,
        interval_sec: intervalSec,
        jitter_sec: jitterSec,
      });
      q.refetch();
      toast.success("Настройки авторежима сохранены");
      onClose();
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Сохранение: ${d.title}`, d.detail);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v && !saving) onClose(); }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-baseline justify-between gap-2">
            <span>Авторежим: {accountLabel}</span>
            {q.data && (
              <span className={
                "mono text-[11px] " +
                (q.data.is_running ? "text-status-success" : "text-muted-foreground")
              }>
                {q.data.is_running ? "▶ running" : "⏸ paused"}
              </span>
            )}
          </DialogTitle>
        </DialogHeader>

        {q.isError && <ErrorBox title="Не удалось загрузить" detail={String(q.error)} />}

        {q.data && (
          <>
            <div className="flex flex-col gap-3">
              <div className="flex flex-col gap-1">
                <Label htmlFor="ar-queue">
                  Очередь юзернеймов
                  <span className="ml-2 text-[10px] text-muted-foreground">
                    по одному на строку, без `@` (бэк всё равно почистит)
                  </span>
                </Label>
                <Textarea
                  id="ar-queue"
                  value={queueText}
                  onChange={(e) => setQueueText(e.target.value)}
                  className="mono min-h-[180px] text-xs"
                  placeholder={"ivan_petrov\nolga_sidorova\n@kirill_2025"}
                />
                <div className="flex justify-between text-[11px] text-muted-foreground">
                  <span>после прочтения юзернейма он удаляется из очереди</span>
                  <span className={queueValid ? "" : "text-destructive"}>
                    {parsedQueue.length} / {MAX_QUEUE}
                  </span>
                </div>
              </div>

              <div className="flex gap-3">
                <div className="flex flex-1 flex-col gap-1">
                  <Label htmlFor="ar-interval">Интервал (сек)</Label>
                  <Input
                    id="ar-interval"
                    type="number"
                    min={MIN_INTERVAL}
                    value={intervalSec}
                    onChange={(e) => setIntervalSec(Number(e.target.value || 0))}
                    className="mono"
                  />
                  <span className="text-[11px] text-muted-foreground">
                    ≈ {formatSeconds(intervalSec)} (минимум {MIN_INTERVAL})
                  </span>
                </div>
                <div className="flex flex-1 flex-col gap-1">
                  <Label htmlFor="ar-jitter">Разброс ± (сек)</Label>
                  <Input
                    id="ar-jitter"
                    type="number"
                    min={0}
                    value={jitterSec}
                    onChange={(e) => setJitterSec(Number(e.target.value || 0))}
                    className="mono"
                  />
                  <span className="text-[11px] text-muted-foreground">
                    случайная добавка к интервалу
                  </span>
                </div>
              </div>

              <div className="rounded-md border border-border bg-secondary/40 p-2 text-[11px]">
                <div className="text-muted-foreground">
                  Каждая пауза будет случайной в диапазоне:
                </div>
                <div className="mono">
                  от <b>{formatSeconds(minDelay)}</b> до <b>{formatSeconds(maxDelay)}</b>
                </div>
              </div>

              {q.data.last_status && (
                <div className="text-[11px] text-muted-foreground">
                  Последнее событие: <span className="mono">{q.data.last_status}</span>
                  {q.data.last_action_at && (
                    <span> · {new Date(q.data.last_action_at).toLocaleString()}</span>
                  )}
                </div>
              )}
            </div>

            <div className="mt-3 flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={onClose} disabled={saving}>
                Отмена
              </Button>
              <Button size="sm" onClick={save} disabled={!formValid || saving}>
                {saving ? "Сохраняем…" : "Сохранить"}
              </Button>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
