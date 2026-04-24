import { Plus } from "lucide-react";
import { cn } from "@/lib/utils";

// Кнопка "+" под списком диалогов в левой колонке. Визуально — dashed
// вариант DialogListItem, чтобы не путался с реальными диалогами.
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
        "m-2 flex items-center justify-center gap-2 rounded-md border-dashed py-3 text-xs font-medium",
        "hairline border-border text-muted-foreground transition-colors",
        disabled
          ? "cursor-not-allowed opacity-50"
          : "hover:border-foreground/60 hover:text-foreground",
      )}
    >
      <Plus className="h-4 w-4" />
      Новый авто-диалог
    </button>
  );
}
