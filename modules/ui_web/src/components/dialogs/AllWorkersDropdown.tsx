import { List } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import { StatusDot } from "@/components/common/StatusDot";
import type { Account } from "@/types/api";

// Дропдаун-список всех воркеров. Удобно когда воркеров много и в
// горизонтальном ряду неудобно листать. Клик по строке — переход
// на этого воркера.

type Props = {
  accounts: Account[];
  currentAccountId: number | null;
  onPick: (accountId: number) => void;
};

export function AllWorkersDropdown({ accounts, currentAccountId, onPick }: Props) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-9 gap-1 px-2 text-xs"
          title="Все воркеры"
        >
          <List className="h-3.5 w-3.5" />
          <span>{accounts.length}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[260px] max-h-[60vh] overflow-y-auto">
        <DropdownMenuLabel>Воркеры — {accounts.length}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {accounts.length === 0 && (
          <div className="px-2 py-2 text-xs text-muted-foreground">пока нет</div>
        )}
        {accounts.map((a) => (
          <DropdownMenuItem
            key={a.id}
            onSelect={() => onPick(a.id)}
            className={cn(
              "flex items-center gap-2",
              a.id === currentAccountId && "bg-accent",
            )}
          >
            <StatusDot status={a.status} />
            <div className="flex min-w-0 flex-1 flex-col">
              <span className="truncate text-xs font-medium">
                {a.name || `account_${a.id}`}
              </span>
              <span className="mono truncate text-[10px] text-muted-foreground">
                {a.phone}
              </span>
            </div>
            <span className="shrink-0 text-[10px] text-muted-foreground">
              {a.dialogs_count}
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
