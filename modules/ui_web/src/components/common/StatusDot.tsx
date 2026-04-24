import { cn } from "@/lib/utils";
import type { EventStatus, WorkerStatus } from "@/types/api";

type AnyStatus = EventStatus | WorkerStatus;

const BG: Record<AnyStatus, string> = {
  success:          "bg-status-success",
  error:            "bg-status-error",
  in_progress:      "bg-status-in_progress",
  running:          "bg-status-success",
  crashed:          "bg-status-error",
  starting:         "bg-status-in_progress",
  stopping:         "bg-status-in_progress",
  stopped:          "bg-status-stopped",
  session_expired:  "bg-status-error",
};

export function StatusDot({ status, className }: { status: AnyStatus; className?: string }) {
  return <span className={cn("inline-block h-2 w-2 rounded-full", BG[status], className)} aria-label={status} />;
}
