import { cn } from "@/lib/utils";
import type { DialogSummary } from "@/types/api";

function initials(d: DialogSummary) {
  const name = d.title?.trim() || d.username || "?";
  return name.slice(0, 2).toUpperCase();
}

function formatTime(iso: string) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function previewText(d: DialogSummary) {
  const m = d.last_message;
  if (!m) return "";
  if (m.text) return m.text;
  return `[${m.type}]`;
}

export function DialogListItem({
  dialog,
  selected,
  onSelect,
}: {
  dialog: DialogSummary;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      onClick={onSelect}
      className={cn(
        "flex w-full items-start gap-2 px-2 py-2 text-left transition-colors hairline border-transparent border-b border-b-border/50",
        selected ? "bg-accent" : "hover:bg-accent/60",
      )}
    >
      <div
        className={cn(
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold",
          dialog.is_bot ? "bg-module-auth/30 text-module-auth" : "bg-secondary text-secondary-foreground",
        )}
      >
        {initials(dialog)}
      </div>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-1">
          {dialog.is_contact && <span className="h-1.5 w-1.5 rounded-full bg-status-success" />}
          <span className="truncate text-sm font-medium">{dialog.title || "(без имени)"}</span>
          {dialog.last_message && (
            <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">{formatTime(dialog.last_message.date)}</span>
          )}
        </div>
        <div className="mono truncate text-[11px] text-muted-foreground">
          {dialog.username ? `@${dialog.username}` : <em className="not-italic opacity-70">без username</em>}
        </div>
        <div className="truncate text-xs text-muted-foreground">{previewText(dialog)}</div>
      </div>
    </button>
  );
}
