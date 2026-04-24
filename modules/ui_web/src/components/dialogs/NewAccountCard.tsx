import { Card, CardContent } from "@/components/ui/card";
import { Plus } from "lucide-react";

// Пустая карточка "добавить аккаунт" в конец ряда аккаунтов.
// Клик открывает NewAccountDialog.
export function NewAccountCard({ onClick }: { onClick: () => void }) {
  return (
    <Card
      onClick={onClick}
      className="min-w-[140px] shrink-0 cursor-pointer border-dashed text-muted-foreground transition-colors hover:border-foreground/50 hover:text-foreground"
    >
      <CardContent className="flex h-full min-h-[74px] flex-col items-center justify-center gap-1 p-3">
        <Plus className="h-4 w-4" />
        <div className="text-xs font-medium">Новый аккаунт</div>
      </CardContent>
    </Card>
  );
}
