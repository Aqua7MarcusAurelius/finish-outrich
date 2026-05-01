import { useState } from "react";
import { ChevronDown, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";
import type { DialogSummary, DialogUserStatus } from "@/types/api";

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

// Конфиг отображения 4 статусов + "без". Цвета берём из существующих
// status-* токенов темы, без новых CSS-переменных.
type StatusOption = {
  value: DialogUserStatus;
  label: string;
  // Tailwind-класс на круглую точку и текст-цвет в дропдауне.
  dotClass: string;
};

const STATUS_OPTIONS: StatusOption[] = [
  { value: "talking", label: "Общаемся",  dotClass: "bg-status-success" },
  { value: "waiting", label: "Ждём ответ", dotClass: "bg-status-in_progress" },
  { value: "done",    label: "Готово",    dotClass: "bg-status-stopped" },
  { value: "failed",  label: "Провал",    dotClass: "bg-status-error" },
];

const NO_STATUS: StatusOption = {
  value: null,
  label: "Без статуса",
  dotClass: "bg-border",
};

function statusOption(value: DialogUserStatus): StatusOption {
  return STATUS_OPTIONS.find((o) => o.value === value) ?? NO_STATUS;
}

export function DialogListItem({
  dialog,
  selected,
  onSelect,
  onStatusChange,
}: {
  dialog: DialogSummary;
  selected: boolean;
  onSelect: () => void;
  onStatusChange?: () => void;
}) {
  const toast = useToast();
  const [pending, setPending] = useState<DialogUserStatus | undefined>(undefined);

  // pending — оптимистичное значение пока летит запрос. Для UI это
  // выглядит как "статус сменился сразу", если PATCH упадёт — откатим.
  const effective: DialogUserStatus = pending !== undefined ? pending : dialog.user_status;
  const opt = statusOption(effective);

  async function setStatus(value: DialogUserStatus) {
    if (value === effective) return;
    const prev = dialog.user_status;
    setPending(value);
    try {
      await api.setDialogStatus(dialog.id, value);
      onStatusChange?.();
    } catch (e) {
      setPending(prev);
      const d = describeApiError(e);
      toast.error(`Статус: ${d.title}`, d.detail);
      return;
    }
    setPending(undefined);
  }

  return (
    <div
      className={cn(
        "relative w-full hairline border-transparent border-b border-b-border/50",
        selected ? "bg-accent" : "hover:bg-accent/60",
      )}
    >
      <button
        onClick={onSelect}
        className="flex w-full items-start gap-2 px-2 py-2 pr-14 text-left"
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

      {/* Pill — абсолютно позиционирована от левого края строки на
         ~280px. Right-based позиционирование иногда уезжало за
         viewport из-за scrollbar/Radix display:table — left-based
         предсказуемо в видимой зоне 320px-aside. */}
      <div className="absolute left-[270px] top-1.5 z-10">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              onClick={(e) => e.stopPropagation()}
              title={`Статус: ${opt.label}`}
              className={cn(
                "flex items-center gap-1 rounded-full border border-border bg-secondary px-1.5 py-0.5",
                "text-foreground hover:bg-background",
              )}
            >
              <span
                className={cn(
                  "h-2.5 w-2.5 rounded-full ring-1 ring-border",
                  effective === null ? "bg-transparent" : opt.dotClass,
                )}
              />
              <ChevronDown className="h-3 w-3 opacity-80" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-[160px]">
            <DropdownMenuLabel>Статус диалога</DropdownMenuLabel>
            <DropdownMenuSeparator />
            {STATUS_OPTIONS.map((o) => (
              <DropdownMenuItem
                key={o.label}
                onSelect={() => setStatus(o.value)}
                className="gap-2"
              >
                <span className={cn("h-2 w-2 rounded-full", o.dotClass)} />
                <span className="flex-1">{o.label}</span>
                {effective === o.value && <Check className="h-3 w-3" />}
              </DropdownMenuItem>
            ))}
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onSelect={() => setStatus(null)}
              className="gap-2 text-muted-foreground"
            >
              <span className={cn("h-2 w-2 rounded-full", NO_STATUS.dotClass)} />
              <span className="flex-1">{NO_STATUS.label}</span>
              {effective === null && <Check className="h-3 w-3" />}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}
