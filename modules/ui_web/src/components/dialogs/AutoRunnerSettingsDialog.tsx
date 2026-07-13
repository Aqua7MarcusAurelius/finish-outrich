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

// Настройки авторежима одного воркера.
//
// Модель: сессия автопрописи = окно `window_hours` часов; при старте
// генерируется N ∈ [min_leads, max_leads] случайных моментов внутри
// окна. Первый лид — не раньше чем через `warmup_minutes` после
// включения. По истечении окна или пустого расписания — авто-стоп.
//
// Save = PATCH конфига. Save НЕ пересобирает уже идущую сессию — новые
// значения применятся только при следующем `start`. Чтобы применить
// сразу — нажать stop → start в UI.

type Props = {
  open: boolean;
  accountId: number | null;
  accountLabel: string;
  onClose: () => void;
};

// Верхние границы дублируем из бэка (см. modules/worker_auto_runner/service.py).
const MAX_WINDOW_HOURS = 48;
const MAX_LEADS = 500;
const MAX_WARMUP_MINUTES = 240;
const MAX_QUEUE = 1000;

function parseQueueText(text: string): string[] {
  return text
    .split(/[\r\n]+/)
    .map((s) => s.trim().replace(/^@/, ""))
    .filter(Boolean);
}

function formatHM(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h === 0) return `${m} мин`;
  if (m === 0) return `${h} ч`;
  return `${h} ч ${m} мин`;
}

export function AutoRunnerSettingsDialog({ open, accountId, accountLabel, onClose }: Props) {
  const toast = useToast();
  const q = useAutoRunner(open ? accountId : null);

  const [queueText, setQueueText] = useState("");
  const [windowHours, setWindowHours] = useState<number>(12);
  const [minLeads, setMinLeads] = useState<number>(15);
  const [maxLeads, setMaxLeads] = useState<number>(18);
  const [warmupMinutes, setWarmupMinutes] = useState<number>(10);
  const [activeStartHourMsk, setActiveStartHourMsk] = useState<number>(8);
  const [activeEndHourMsk, setActiveEndHourMsk] = useState<number>(23);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    const d = q.data;
    if (!d) return;
    setQueueText(d.queue.join("\n"));
    setWindowHours(d.window_hours);
    setMinLeads(d.min_leads);
    setMaxLeads(d.max_leads);
    setWarmupMinutes(d.warmup_minutes);
    setActiveStartHourMsk(d.active_start_hour_msk);
    setActiveEndHourMsk(d.active_end_hour_msk);
  }, [open, q.data]);

  const parsedQueue = useMemo(() => parseQueueText(queueText), [queueText]);

  const activeHoursValid =
    activeStartHourMsk >= 0 && activeStartHourMsk <= 23 &&
    activeEndHourMsk >= 1 && activeEndHourMsk <= 24 &&
    activeEndHourMsk > activeStartHourMsk;
  const leadsValid =
    minLeads >= 1 && maxLeads >= minLeads && maxLeads <= MAX_LEADS;
  const warmupValid = warmupMinutes >= 0 && warmupMinutes <= MAX_WARMUP_MINUTES;
  const queueValid = parsedQueue.length <= MAX_QUEUE;
  const formValid = activeHoursValid && leadsValid && warmupValid && queueValid;

  const windowHrs = activeEndHourMsk - activeStartHourMsk;
  const avgLeads = (minLeads + maxLeads) / 2;
  const avgIntervalMin =
    avgLeads > 0 ? Math.round((windowHrs * 60) / avgLeads) : 0;

  async function save() {
    if (accountId == null || !formValid) return;
    setSaving(true);
    try {
      await api.patchAutoRunner(accountId, {
        queue: parsedQueue,
        min_leads: minLeads,
        max_leads: maxLeads,
        warmup_minutes: warmupMinutes,
        active_start_hour_msk: activeStartHourMsk,
        active_end_hour_msk: activeEndHourMsk,
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
                  className="mono min-h-[140px] text-xs"
                  placeholder={"ivan_petrov\nolga_sidorova\n@kirill_2025"}
                />
                <div className="flex justify-between text-[11px] text-muted-foreground">
                  <span>после старта диалога юзернейм удаляется из очереди</span>
                  <span className={queueValid ? "" : "text-destructive"}>
                    {parsedQueue.length} / {MAX_QUEUE}
                  </span>
                </div>
              </div>

              <div className="flex gap-3">
                <div className="flex flex-1 flex-col gap-1">
                  <Label htmlFor="ar-start">Начало окна (МСК)</Label>
                  <Input
                    id="ar-start"
                    type="number"
                    min={0}
                    max={23}
                    value={activeStartHourMsk}
                    onChange={(e) => setActiveStartHourMsk(Number(e.target.value || 0))}
                    className="mono"
                  />
                  <span className="text-[11px] text-muted-foreground">
                    час (0–23), например 8 = 08:00
                  </span>
                </div>
                <div className="flex flex-1 flex-col gap-1">
                  <Label htmlFor="ar-end">Конец окна (МСК)</Label>
                  <Input
                    id="ar-end"
                    type="number"
                    min={1}
                    max={24}
                    value={activeEndHourMsk}
                    onChange={(e) => setActiveEndHourMsk(Number(e.target.value || 0))}
                    className="mono"
                  />
                  <span className="text-[11px] text-muted-foreground">
                    час (1–24), например 23 = 23:00
                  </span>
                </div>
                <div className="flex flex-1 flex-col gap-1">
                  <Label htmlFor="ar-warmup">Прогрев (мин)</Label>
                  <Input
                    id="ar-warmup"
                    type="number"
                    min={0}
                    max={MAX_WARMUP_MINUTES}
                    value={warmupMinutes}
                    onChange={(e) => setWarmupMinutes(Number(e.target.value || 0))}
                    className="mono"
                  />
                  <span className="text-[11px] text-muted-foreground">
                    первый лид не раньше
                  </span>
                </div>
              </div>

              <div className="flex gap-3">
                <div className="flex flex-1 flex-col gap-1">
                  <Label htmlFor="ar-min">Мин. лидов</Label>
                  <Input
                    id="ar-min"
                    type="number"
                    min={1}
                    max={MAX_LEADS}
                    value={minLeads}
                    onChange={(e) => setMinLeads(Number(e.target.value || 0))}
                    className="mono"
                  />
                </div>
                <div className="flex flex-1 flex-col gap-1">
                  <Label htmlFor="ar-max">Макс. лидов</Label>
                  <Input
                    id="ar-max"
                    type="number"
                    min={1}
                    max={MAX_LEADS}
                    value={maxLeads}
                    onChange={(e) => setMaxLeads(Number(e.target.value || 0))}
                    className="mono"
                  />
                </div>
              </div>

              <div className="rounded-md border border-border bg-secondary/40 p-2 text-[11px]">
                <div className="text-muted-foreground">
                  При старте: разбросаем <b>{minLeads}–{maxLeads}</b> лидов
                  случайно по окну <b>{activeStartHourMsk}:00–{activeEndHourMsk}:00 МСК</b> ({formatHM(windowHrs * 60)}). Первый
                  лид — не раньше <b>{formatHM(warmupMinutes)}</b>. В среднем
                  один лид примерно каждые <b>{formatHM(avgIntervalMin)}</b>.
                </div>
                {q.data.is_running && (
                  <div className="mt-1 text-status-warning">
                    Сессия уже идёт — новые значения применятся при
                    следующем <b>Start</b>. Чтобы применить сразу — сначала <b>Stop</b>.
                  </div>
                )}
              </div>

              {q.data.next_dispatch_at && q.data.is_running && (
                <div className="text-[11px] text-muted-foreground">
                  Следующий лид: <span className="mono">
                    {new Date(q.data.next_dispatch_at).toLocaleTimeString()}
                  </span>{" · осталось "}
                  <span className="mono">{q.data.schedule_remaining}</span>
                </div>
              )}

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
