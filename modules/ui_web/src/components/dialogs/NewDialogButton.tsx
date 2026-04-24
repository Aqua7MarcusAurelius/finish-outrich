import { Plus } from "lucide-react";
import { cn } from "@/lib/utils";

// Кнопка "+" прижата к низу левой колонки и занимает её полную ширину.
// Лежит вне ScrollArea (см. DialogsPage), поэтому остаётся на месте при
// прокрутке списка диалогов — работает как sticky footer секции.
export function NewDialogButton({
  onClick,
  disabled,
  hint,
}: {
  onClick: () => void;
  disabled?: boolean;
  hint?: string;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={disabled ? hint : "Запустить авто-диалог (AutoChat)"}
      className={cn(
        "flex w-full shrink-0 items-center justify-center gap-2 border-t border-border bg-background px-3 py-3 text-xs font-medium",
        "text-muted-foreground transition-colors",
        disabled
          ? "cursor-not-allowed opacity-50"
          : "hover:bg-accent/60 hover:text-foreground",
      )}
    >
      <Plus className="h-4 w-4" />
      Новый авто-диалог
    </button>
  );
}
