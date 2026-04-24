import { cn } from "@/lib/utils";
import { ModulePill } from "@/components/common/ModulePill";
import { StatusDot } from "@/components/common/StatusDot";
import type { BusEvent } from "@/types/api";

function fmt(iso: string) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

type Props = {
  ev: BusEvent;
  isNew?: boolean;
  onOpen?: (id: number) => void;
  onQuickFilter?: (patch: { account?: number; module?: string; type?: string; status?: string }) => void;
};

export function EventRow({ ev, isNew, onOpen, onQuickFilter }: Props) {
  return (
    <div
      className={cn(
        "grid grid-cols-[70px_22px_90px_130px_170px_60px_1fr] items-center gap-2 px-2 py-1 text-xs hairline border-transparent border-b border-b-border/40",
        ev.status === "error" && "bg-status-error/10",
        isNew && "animate-flash-new",
      )}
    >
      <button className="mono text-muted-foreground hover:text-foreground" onClick={() => onOpen?.(ev.id)}>#{ev.id}</button>
      <button onClick={() => onQuickFilter?.({ status: ev.status })}>
        <StatusDot status={ev.status} />
      </button>
      <span className="mono text-muted-foreground">{fmt(ev.time)}</span>
      <button
        className="mono truncate text-left hover:text-foreground"
        onClick={() => ev.account_id != null && onQuickFilter?.({ account: ev.account_id })}
      >
        {ev.account_name || (ev.account_id != null ? `account_${ev.account_id}` : "—")}
      </button>
      <ModulePill module={ev.module} onClick={() => onQuickFilter?.({ module: ev.module })} />
      <button
        className="mono text-muted-foreground hover:text-foreground"
        onClick={() => ev.parent_id && onOpen?.(ev.parent_id)}
      >
        {ev.parent_id ? `→ #${ev.parent_id}` : "—"}
      </button>
      <div className="min-w-0 truncate">
        <button className="mono font-semibold hover:underline" onClick={() => onQuickFilter?.({ type: ev.type })}>
          {ev.type}
        </button>
        {ev.message && <span className="ml-2 text-muted-foreground">{ev.message}</span>}
      </div>
    </div>
  );
}
