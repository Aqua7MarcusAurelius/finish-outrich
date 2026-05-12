import { useState, useEffect } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AlertTriangle } from "lucide-react";

// Подтверждение массового удаления ВСЕХ диалогов аккаунта.
// Двойная защита: 1) сам факт открытия этой модалки, 2) обязательный
// ввод строки "CONFIRM" в поле — кнопка активируется только при точном
// совпадении. Бэк независимо проверяет ту же строку в query-параметре.

const REQUIRED = "CONFIRM";

type Props = {
  open: boolean;
  accountLabel: string;        // имя/телефон чтобы оператор точно понял какой аккаунт
  dialogsCount: number;        // сколько диалогов будет затронуто
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

export function WipeDialogsConfirm({
  open, accountLabel, dialogsCount, busy, onConfirm, onCancel,
}: Props) {
  const [value, setValue] = useState("");

  // При закрытии чистим поле, чтобы при повторном открытии не было
  // случайно «уже подтверждено».
  useEffect(() => { if (!open) setValue(""); }, [open]);

  const matches = value === REQUIRED;

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v && !busy) onCancel(); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-destructive" />
            Удалить ВСЕ диалоги аккаунта?
          </DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-2 text-xs">
          <div className="text-sm">
            Аккаунт: <span className="font-semibold">{accountLabel}</span>
          </div>
          <div className="text-muted-foreground">
            Будет затронуто <span className="font-semibold">{dialogsCount}</span> диалогов.
            Для каждого:
          </div>
          <ul className="list-disc pl-5 text-muted-foreground">
            <li>
              <span className="font-semibold">Удаление в Telegram с revoke=true</span> —
              переписка пропадёт и у собеседника тоже.
            </li>
            <li>Локальные сообщения, медиа и реакции стираются каскадом.</li>
            <li>Активные авто-сессии останавливаются.</li>
            <li>Файлы вложений в MinIO удаляются.</li>
          </ul>
          <div className="mt-1 font-semibold text-destructive">
            Действие необратимо. Воркер должен быть запущен.
          </div>
        </div>

        <div className="mt-3 flex flex-col gap-1.5">
          <label className="text-xs text-muted-foreground">
            Для подтверждения введите <span className="font-mono font-semibold">{REQUIRED}</span>
          </label>
          <Input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            disabled={busy}
            placeholder={REQUIRED}
            autoFocus
            className="font-mono"
          />
        </div>

        <div className="mt-3 flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy}>
            Отмена
          </Button>
          <Button
            size="sm"
            onClick={onConfirm}
            disabled={busy || !matches}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            {busy ? "Удаляем…" : "Удалить всё"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
