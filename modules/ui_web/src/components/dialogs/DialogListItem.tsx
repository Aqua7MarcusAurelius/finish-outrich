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
import { useDialogStatuses } from "@/hooks/useAccounts";
import type { DialogSummary, DialogStatusRef } from "@/types/api";

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
  onStatusChange,
}: {
  dialog: DialogSummary;
  selected: boolean;
  onSelect: () => void;
  onStatusChange?: () => void;
}) {
  const toast = useToast();
  const catalog = useDialogStatuses().data ?? [];

  // pending — оптимистичное значение пока летит PATCH. undefined = нет
  // пендинга, null = пендинг «снять статус», number = пендинг id.
  // Полную карточку (label/color) для рендера находим в catalog.
  const [pendingId, setPendingId] = useState<number | null | undefined>(undefined);

  const effectiveId: number | null =
    pendingId !== undefined ? pendingId : (dialog.status?.id ?? null);

  const effective: DialogStatusRef | null =
    pendingId === undefined
      ? dialog.status
      : pendingId === null
        ? null
        : (catalog.find((s) => s.id === pendingId) ?? null);

  async function setStatus(value: number | null) {
    if (value === effectiveId) return;
    const prevId = dialog.status?.id ?? null;
    setPendingId(value);
    try {
      await api.setDialogStatus(dialog.id, value);
      onStatusChange?.();
    } catch (e) {
      setPendingId(prevId);
      const d = describeApiError(e);
      toast.error(`Статус: ${d.title}`, d.detail);
      return;
    }
    setPendingId(undefined);
  }

  const sortedCatalog = [...catalog].sort(
    (a, b) => a.sort_order - b.sort_order || a.id - b.id,
  );

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
         предсказуемо в видимой зоне 320px-aside.
         Цвет точки и список опций тянутся из каталога dialog_statuses. */}
      <div className="absolute left-[270px] top-1.5 z-10">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              onClick={(e) => e.stopPropagation()}
              title={effective ? `Статус: ${effective.label}` : "Без статуса"}
              className={cn(
                "flex items-center gap-1 rounded-full border border-border bg-secondary px-1.5 py-0.5",
                "text-foreground hover:bg-background",
              )}
            >
              <span
                className="h-2.5 w-2.5 rounded-full ring-1 ring-border"
                style={{ backgroundColor: effective?.color ?? "transparent" }}
              />
              <ChevronDown className="h-3 w-3 opacity-80" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-[180px]">
            <DropdownMenuLabel>Статус диалога</DropdownMenuLabel>
            <DropdownMenuSeparator />
            {sortedCatalog.length === 0 && (
              <DropdownMenuItem disabled className="text-xs text-muted-foreground">
                Каталог пуст — заведи статус в «Настройки».
              </DropdownMenuItem>
            )}
            {sortedCatalog.map((o) => (
              <DropdownMenuItem
                key={o.id}
                onSelect={() => setStatus(o.id)}
                className="gap-2"
              >
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ backgroundColor: o.color }}
                />
                <span className="flex-1 truncate">{o.label}</span>
                {effectiveId === o.id && <Check className="h-3 w-3" />}
              </DropdownMenuItem>
            ))}
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onSelect={() => setStatus(null)}
              className="gap-2 text-muted-foreground"
            >
              <span className="h-2 w-2 rounded-full bg-border" />
              <span className="flex-1">Без статуса</span>
              {effectiveId === null && <Check className="h-3 w-3" />}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}
