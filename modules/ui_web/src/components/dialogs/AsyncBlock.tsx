import type { AsyncJobStatus } from "@/types/api";
import { cn } from "@/lib/utils";

type Props = {
  label: string;
  status: AsyncJobStatus;
  text: string | null;
  className?: string;
};

const TEXT_BY_STATUS: Record<AsyncJobStatus, string> = {
  none: "",
  pending: "обрабатывается…",
  failed: "не удалось распознать",
  done: "тишина или неразборчиво",
};

export function AsyncBlock({ label, status, text, className }: Props) {
  if (status === "none") return null;
  const display = status === "done" ? (text?.trim() ? text : TEXT_BY_STATUS.done) : TEXT_BY_STATUS[status];
  return (
    <div className={cn("mt-1 rounded-sm bg-secondary/60 p-2 text-xs", className)}>
      <div className="mono mb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={cn(status === "failed" && "text-destructive", status === "pending" && "opacity-70 italic")}>
        {display}
      </div>
    </div>
  );
}
