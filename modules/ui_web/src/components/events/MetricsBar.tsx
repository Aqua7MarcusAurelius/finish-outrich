import { Card, CardContent } from "@/components/ui/card";
import type { EventStats } from "@/types/api";
import { cn } from "@/lib/utils";

function Metric({ label, value, tone }: { label: string; value: string | number; tone?: "error" | "success" | "warn" }) {
  return (
    <Card className={cn(tone === "error" && Number(value) > 0 && "border-status-error/60")}>
      <CardContent className="p-2">
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
        <div
          className={cn(
            "mono text-lg font-semibold",
            tone === "error"   && Number(value) > 0 && "text-status-error",
            tone === "success" && "text-status-success",
            tone === "warn"    && "text-status-in_progress",
          )}
        >
          {value}
        </div>
      </CardContent>
    </Card>
  );
}

export function MetricsBar({ stats, periodLabel }: { stats: EventStats | undefined; periodLabel: string }) {
  const s = stats ?? { total: 0, success: 0, error: 0, in_progress: 0, events_per_sec: 0 };
  return (
    <div className="grid grid-cols-5 gap-2">
      <Metric label={`Всего · ${periodLabel}`} value={s.total} />
      <Metric label="Success" value={s.success} tone="success" />
      <Metric label="Error" value={s.error} tone="error" />
      <Metric label="In progress" value={s.in_progress} tone="warn" />
      <Metric label="Events / sec" value={s.events_per_sec.toFixed(2)} />
    </div>
  );
}
