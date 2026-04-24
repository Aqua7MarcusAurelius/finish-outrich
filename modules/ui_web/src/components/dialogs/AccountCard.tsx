import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusDot } from "@/components/common/StatusDot";
import { cn } from "@/lib/utils";
import { Play, Square, RefreshCw } from "lucide-react";
import type { Account } from "@/types/api";

type Props = {
  account: Account;
  selected: boolean;
  onSelect: () => void;
  onStart?: () => void;
  onStop?: () => void;
};

export function AccountCard({ account, selected, onSelect, onStart, onStop }: Props) {
  const needsReauth = account.status === "session_expired";
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
          <div className="flex gap-1">
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
