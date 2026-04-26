import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { AlertTriangle } from "lucide-react";

// Подтверждение жёсткого удаления диалога. Действие необратимо:
// сообщения, медиа и активная авто-сессия пропадут навсегда.

type Props = {
  open: boolean;
  dialogTitle: string;   // имя/username чтобы оператор точно понимал кого удаляет
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

export function DeleteDialogConfirm({ open, dialogTitle, busy, onConfirm, onCancel }: Props) {
  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v && !busy) onCancel(); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-destructive" />
            Удалить диалог?
          </DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-2 text-xs">
          <div className="text-sm">
            Диалог с <span className="font-semibold">{dialogTitle}</span>
          </div>
          <div className="text-muted-foreground">
            Будут удалены навсегда:
          </div>
          <ul className="list-disc pl-5 text-muted-foreground">
            <li>Все сообщения и медиа этого диалога</li>
            <li>Активная авто-сессия (если запущена) — будет остановлена</li>
            <li>Файлы вложений в MinIO</li>
          </ul>
          <div className="mt-1 text-muted-foreground">
            Для системы собеседник станет «новым»: следующее сообщение от/к
            нему создаст диалог с пустой историей.
          </div>
          <div className="mt-1 font-semibold text-destructive">
            Действие необратимо.
          </div>
        </div>

        <div className="mt-3 flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy}>
            Отмена
          </Button>
          <Button
            size="sm"
            onClick={onConfirm}
            disabled={busy}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            {busy ? "Удаляем…" : "Удалить"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
