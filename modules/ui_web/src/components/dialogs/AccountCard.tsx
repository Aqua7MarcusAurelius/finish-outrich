import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusDot } from "@/components/common/StatusDot";
import { cn } from "@/lib/utils";
import { Play, Pause, Settings, Square, RefreshCw, FileText, Trash2 } from "lucide-react";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";
import { useAutoRunner } from "@/hooks/useAccounts";
import type { Account } from "@/types/api";

type Props = {
  account: Account;
  selected: boolean;
  onSelect: () => void;
  onStart?: () => void;
  onStop?: () => void;
  onEditPrompt?: () => void;
  onWipeDialogs?: () => void;
  // Открыть модалку настроек авторежима для этого account_id.
  // Сам тогл (Play/Pause) карточка обрабатывает сама — он по сути
  // одна ручка, нет смысла гонять колбэк через parent.
  onAutoRunnerSettings?: () => void;
};

export function AccountCard({
  account, selected, onSelect, onStart, onStop, onEditPrompt, onWipeDialogs,
  onAutoRunnerSettings,
}: Props) {
  const toast = useToast();
  const qc = useQueryClient();
  const arQ = useAutoRunner(account.id);
  const [arBusy, setArBusy] = useState(false);

  const needsReauth = account.status === "session_expired";
  const workerRunning = account.status === "running";
  const wipeEnabled = workerRunning && account.dialogs_count > 0;
  const arRunning = !!arQ.data?.is_running;
  // Pause возможен ВСЕГДА когда автомат бежит (даже если воркер
  // отвалился — это сценарий «погасить руками»). Start — только при
  // живом воркере (бэк всё равно отдаст 409, но disabled-вид аккуратнее).
  const arToggleEnabled = (workerRunning || arRunning) && !arBusy;

  async function toggleAuto() {
    setArBusy(true);
    try {
      if (arRunning) {
        await api.stopAutoRunner(account.id);
      } else {
        await api.startAutoRunner(account.id);
      }
      await qc.invalidateQueries({ queryKey: ["auto-runner", account.id] });
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Авторежим: ${d.title}`, d.detail);
    } finally {
      setArBusy(false);
    }
  }

  return (
    <Card
      onClick={onSelect}
      className={cn(
        "min-w-[220px] shrink-0 cursor-pointer hairline transition-colors",
        selected ? "border-ring" : "hover:border-accent-foreground/30",
      )}
    >
      <CardContent className="flex flex-col gap-1 p-3">
        <div className="flex items-center gap-2">
          <StatusDot status={account.status} />
          <span className="truncate text-sm font-semibold">{account.name || `account_${account.id}`}</span>
          {arRunning && (
            <span
              className="ml-auto inline-flex items-center gap-1 rounded-full bg-status-success/15 px-1.5 py-0.5 text-[10px] font-semibold text-status-success"
              title="Авторежим запущен"
            >
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-status-success" />
              авто
            </span>
          )}
        </div>
        <div className="mono text-xs text-muted-foreground">{account.phone}</div>
        {arRunning && arQ.data?.next_dispatch_at && (
          <div className="mono text-[10px] text-muted-foreground" title="Следующий лид · осталось / всего">
            → {new Date(arQ.data.next_dispatch_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            {" · "}
            {arQ.data.schedule_remaining} осталось
          </div>
        )}
        <div className="mt-1 flex items-center justify-between">
          <span className="text-xs text-muted-foreground">{account.dialogs_count} диалогов</span>
          <div className="flex flex-col items-end gap-1">
            <Button
              size="sm"
              variant="outline"
              onClick={(e) => { e.stopPropagation(); onWipeDialogs?.(); }}
              disabled={!wipeEnabled}
              title={wipeEnabled
                ? "Удалить ВСЕ диалоги аккаунта (Telegram + локально)"
                : "Доступно только для запущенного воркера с диалогами"}
              className="text-destructive hover:bg-destructive/10"
            >
              <Trash2 className="h-3 w-3" /> Wipe
            </Button>

            <div className="flex gap-1">
              <Button
                size="sm"
                variant="outline"
                onClick={(e) => { e.stopPropagation(); toggleAuto(); }}
                disabled={!arToggleEnabled}
                title={arRunning
                  ? "Остановить авторежим"
                  : workerRunning
                    ? "Запустить авторежим"
                    : "Сначала запусти воркер"}
                className={arRunning ? "text-status-success hover:bg-status-success/10" : ""}
              >
                {arRunning
                  ? <><Pause className="h-3 w-3" /> Пауза</>
                  : <><Play className="h-3 w-3" /> Play</>}
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={(e) => { e.stopPropagation(); onAutoRunnerSettings?.(); }}
                title="Настройки авторежима (очередь, окно, лидов)"
              >
                <Settings className="h-3 w-3" />
              </Button>
            </div>

            <Button
              size="sm"
              variant="outline"
              onClick={(e) => { e.stopPropagation(); onEditPrompt?.(); }}
              title="Редактор промта воркера"
            >
              <FileText className="h-3 w-3" /> Промт
            </Button>
            {account.status === "running" ? (
              <Button size="sm" variant="outline" onClick={(e) => { e.stopPropagation(); onStop?.(); }}>
                <Square className="h-3 w-3" /> Stop
              </Button>
            ) : needsReauth ? (
              <Button size="sm" variant="outline" onClick={(e) => { e.stopPropagation(); onStart?.(); }}>
                <RefreshCw className="h-3 w-3" /> Reauth
              </Button>
            ) : (
              <Button size="sm" variant="outline" onClick={(e) => { e.stopPropagation(); onStart?.(); }}>
                <Play className="h-3 w-3" /> Start
              </Button>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
