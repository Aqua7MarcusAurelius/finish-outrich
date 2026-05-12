import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusDot } from "@/components/common/StatusDot";
import { cn } from "@/lib/utils";
import { Play, Square, RefreshCw, FileText, Trash2 } from "lucide-react";
import type { Account } from "@/types/api";

type Props = {
  account: Account;
  selected: boolean;
  onSelect: () => void;
  onStart?: () => void;
  onStop?: () => void;
  onEditPrompt?: () => void;
  onWipeDialogs?: () => void;
};

export function AccountCard({
  account, selected, onSelect, onStart, onStop, onEditPrompt, onWipeDialogs,
}: Props) {
  const needsReauth = account.status === "session_expired";
  // Кнопка wipe активна только если воркер запущен — иначе wrapper.delete_dialog
  // не сработает и бэк вернёт WORKER_NOT_RUNNING. Не блокируем сам клик,
  // но визуально приглушаем чтобы не путать оператора.
  const wipeEnabled = account.status === "running" && account.dialogs_count > 0;
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
        </div>
        <div className="mono text-xs text-muted-foreground">{account.phone}</div>
        <div className="mt-1 flex items-center justify-between">
          <span className="text-xs text-muted-foreground">{account.dialogs_count} диалогов</span>
          <div className="flex flex-col items-end gap-1">
            {/* Wipe — над «Промт» по запросу. Деструктивный акцент,
                чтобы случайно не нажать. Реальное удаление — через
                модалку с обязательным вводом CONFIRM. */}
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
