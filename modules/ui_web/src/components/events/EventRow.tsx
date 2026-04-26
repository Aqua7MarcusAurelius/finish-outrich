import { cn } from "@/lib/utils";
import { ModulePill } from "@/components/common/ModulePill";
import { StatusDot } from "@/components/common/StatusDot";
import type { BusEvent, EventFilters } from "@/types/api";

function fmt(iso: string) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function shortId(id: string | null | undefined): string {
  if (!id) return "";
  return id.slice(0, 8);
}

type Props = {
  ev: BusEvent;
  isNew?: boolean;
  onOpen?: (id: string) => void;
  onQuickFilter?: (patch: Partial<EventFilters>) => void;
};

// Column layout:
//   [  id   ][●][  time  ][account][module  ][ parent ][ type + message ]
//    96px   16  80px     110px   auto      96px       1fr
// min-w-0 on each cell disables CSS-grid's default "min-width: auto = content
// size" rule, so long text actually truncates instead of blowing the row out.
export function EventRow({ ev, isNew, onOpen, onQuickFilter }: Props) {
  return (
    <div
      className={cn(
        "grid grid-cols-[96px_16px_80px_110px_auto_96px_minmax(0,1fr)] items-center gap-2 px-2 py-1 text-xs hairline border-transparent border-b border-b-border/40",
        ev.status === "error" && "bg-status-error/10",
        isNew && "animate-flash-new",
      )}
    >
      <button
        className="mono min-w-0 truncate text-left text-muted-foreground hover:text-foreground"
        onClick={() => onOpen?.(ev.id)}
        title={ev.id}
      >
        #{shortId(ev.id)}
      </button>

      <button onClick={() => onQuickFilter?.({ status: ev.status })} className="flex items-center justify-center">
        <StatusDot status={ev.status} />
      </button>

      <span className="mono min-w-0 truncate text-muted-foreground">{fmt(ev.time)}</span>

      <button
        className="mono min-w-0 truncate text-left hover:text-foreground"
        onClick={() => ev.account_id != null && onQuickFilter?.({ account: ev.account_id })}
        title={ev.account_name || (ev.account_id != null ? `account_${ev.account_id}` : "—")}
      >
        {ev.account_name || (ev.account_id != null ? `account_${ev.account_id}` : "—")}
      </button>

      <div className="min-w-0 overflow-hidden">
        <ModulePill module={ev.module} onClick={() => onQuickFilter?.({ module: ev.module })} />
      </div>

      <button
        className="mono min-w-0 truncate text-left text-muted-foreground hover:text-foreground"
        onClick={() => ev.parent_id && onOpen?.(ev.parent_id)}
        title={ev.parent_id ?? undefined}
      >
        {ev.parent_id ? `→ #${shortId(ev.parent_id)}` : "—"}
      </button>

      <div className="min-w-0 truncate">
        <button
          className="mono font-semibold hover:underline"
          onClick={() => onQuickFilter?.({ type: ev.type })}
        >
          {ev.type}
        </button>
        {ev.message && <span className="ml-2 text-muted-foreground">{ev.message}</span>}
      </div>
    </div>
  );
}
