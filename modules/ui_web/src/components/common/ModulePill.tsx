import { cn } from "@/lib/utils";
import type { BusModule } from "@/types/api";

const BG: Record<BusModule, string> = {
  telegram:       "bg-module-telegram/20 text-module-telegram",
  history:        "bg-module-history/20 text-module-history",
  transcription:  "bg-module-transcription/20 text-module-transcription",
  description:    "bg-module-description/20 text-module-description",
  auth:           "bg-module-auth/20 text-module-auth",
  worker_manager: "bg-module-worker_manager/20 text-module-worker_manager",
  autochat:       "bg-module-autochat/20 text-module-autochat",
};

export function ModulePill({ module, className, onClick }: { module: BusModule; className?: string; onClick?: () => void }) {
  return (
    <span
      onClick={onClick}
      className={cn(
        "mono inline-flex items-center rounded-sm px-1.5 py-0.5 text-[11px] font-medium",
        BG[module],
        onClick && "cursor-pointer hover:brightness-110",
        className,
      )}
    >
      {module}
    </span>
  );
}
