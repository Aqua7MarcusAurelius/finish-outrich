import { cn } from "@/lib/utils";
import type { BusModule } from "@/types/api";

// Known modules get their designated color (see docs/ui/web_ui_overview_v1.md).
// Everything else — unknown module emitted by a new component we haven't taught
// the UI about yet — renders in a neutral fallback so the row still ships.
const BG: Record<BusModule, string> = {
  wrapper:        "bg-module-wrapper/20 text-module-wrapper",
  history:        "bg-module-history/20 text-module-history",
  history_sync:   "bg-module-history_sync/20 text-module-history_sync",
  transcription:  "bg-module-transcription/20 text-module-transcription",
  description:    "bg-module-description/20 text-module-description",
  auth:           "bg-module-auth/20 text-module-auth",
  worker:         "bg-module-worker/20 text-module-worker",
  worker_manager: "bg-module-worker_manager/20 text-module-worker_manager",
  cleaner:        "bg-module-cleaner/20 text-module-cleaner",
  api:            "bg-module-api/20 text-module-api",
  system:         "bg-module-system/20 text-module-system",
  bus:            "bg-module-bus/20 text-module-bus",
  autochat:       "bg-module-autochat/20 text-module-autochat",
};

const FALLBACK = "bg-module-unknown/20 text-module-unknown";

export function ModulePill({ module, className, onClick }: { module: string; className?: string; onClick?: () => void }) {
  const color = BG[module as BusModule] ?? FALLBACK;
  return (
    <span
      onClick={onClick}
      className={cn(
        "mono inline-flex items-center rounded-sm px-1.5 py-0.5 text-[11px] font-medium",
        color,
        onClick && "cursor-pointer hover:brightness-110",
        className,
      )}
    >
      {module}
    </span>
  );
}
