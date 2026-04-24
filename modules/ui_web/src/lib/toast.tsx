import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

// Минималистичный toast — без внешних либ. Нужен чтобы worker-action'ы
// (start/stop) и прочие пользовательские операции не проваливались в
// молчание при 409/5xx из API.

type ToastTone = "error" | "success" | "info";
type Toast = { id: number; tone: ToastTone; title: string; detail?: string };

type ToastApi = {
  error: (title: string, detail?: string) => void;
  success: (title: string, detail?: string) => void;
  info: (title: string, detail?: string) => void;
};

const Ctx = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const api = useContext(Ctx);
  if (!api) throw new Error("useToast must be used inside <ToastProvider>");
  return api;
}

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const push = useCallback((tone: ToastTone, title: string, detail?: string) => {
    const id = nextId++;
    setToasts((prev) => [...prev, { id, tone, title, detail }]);
    // error-тосты живут дольше чтобы было время прочитать
    setTimeout(() => dismiss(id), tone === "error" ? 8000 : 4000);
  }, [dismiss]);

  const api: ToastApi = {
    error:   (t, d) => push("error", t, d),
    success: (t, d) => push("success", t, d),
    info:    (t, d) => push("info", t, d),
  };

  return (
    <Ctx.Provider value={api}>
      {children}
      <div className="pointer-events-none fixed bottom-3 right-3 z-[100] flex max-w-sm flex-col gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={cn(
              "pointer-events-auto flex items-start gap-2 rounded-md p-2 text-xs shadow-md hairline",
              t.tone === "error"   && "bg-destructive/15 border-destructive/60 text-destructive",
              t.tone === "success" && "bg-status-success/15 border-status-success/60",
              t.tone === "info"    && "bg-accent border-border",
            )}
          >
            <div className="flex-1">
              <div className="font-semibold">{t.title}</div>
              {t.detail && <div className="mono mt-0.5 break-all opacity-80">{t.detail}</div>}
            </div>
            <button onClick={() => dismiss(t.id)} className="opacity-60 hover:opacity-100">
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}
