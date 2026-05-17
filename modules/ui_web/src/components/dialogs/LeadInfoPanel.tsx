import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, XCircle, Loader2, Sparkles } from "lucide-react";
import type { DialogProfile, LeadEvaluation } from "@/types/api";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";
import { Button } from "@/components/ui/button";

// Панель информации о лиде (10.10). Показывается в правой части DialogsPage
// под виджетом событий. Содержит:
//   • метки источника: CRM-лид / ручной + пол + причина закрытия
//   • вердикт оценщика и извлечённые поля (если есть)
//   • кнопка "Завершить и оценить" (ручной запуск оценщика)

type Props = {
  dialog: DialogProfile;
};

export function LeadInfoPanel({ dialog }: Props) {
  const toast = useToast();
  const qc = useQueryClient();
  const [evaluating, setEvaluating] = useState(false);

  const evalQ = useQuery({
    queryKey: ["lead-evaluation", dialog.id],
    queryFn: () => api.getEvaluation(dialog.id),
    refetchInterval: 10_000, // если оценщик отработает async — подхватим
  });

  const evaluation: LeadEvaluation | null = evalQ.data?.evaluation ?? null;
  const fromCrm = (dialog as { from_crm?: boolean }).from_crm === true;
  const gender = (dialog as { gender?: string | null }).gender ?? null;

  async function runEvaluator() {
    setEvaluating(true);
    try {
      await api.evaluateDialog(dialog.id);
      toast.success("Оценка запущена", "Результат появится через несколько секунд");
      evalQ.refetch();
      // Может также измениться status — обновим
      qc.invalidateQueries({ queryKey: ["dialog", dialog.id] });
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Оценка: ${d.title}`, d.detail);
    } finally {
      setEvaluating(false);
    }
  }

  return (
    <div className="flex flex-col gap-3 border-b border-border bg-card p-3">
      <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Лид
      </div>

      <div className="flex flex-wrap gap-1.5">
        <Badge label={fromCrm ? "Из CRM" : "Ручной"} tone={fromCrm ? "info" : "muted"} />
        {gender && (
          <Badge
            label={gender === "female" ? "Женский" : gender === "male" ? "Мужской" : gender}
            tone="muted"
          />
        )}
        {(dialog as { partner_crm_terminal_reason?: string | null }).partner_crm_terminal_reason && (
          <Badge
            label={`→ CRM: ${(dialog as { partner_crm_terminal_reason: string }).partner_crm_terminal_reason}`}
            tone="warn"
          />
        )}
      </div>

      {/* Блок оценки */}
      {evaluation ? (
        <EvaluationBlock e={evaluation} />
      ) : (
        <div className="rounded-md border border-dashed border-border bg-background/40 p-3 text-[11px] text-muted-foreground">
          Оценщик ещё не отработал. Запустится автоматом когда LLM поставит
          <span className="mono">&lt;finishdialog/&gt;</span> или нажми кнопку
          справа.
        </div>
      )}

      <div className="flex justify-end">
        <Button
          variant="outline"
          size="sm"
          onClick={runEvaluator}
          disabled={evaluating}
          className="h-7 gap-1.5 px-2 text-[11px]"
          title="Запустить оценщика по текущей переписке (LLM-судья выдаст вердикт)"
        >
          {evaluating
            ? <Loader2 className="h-3 w-3 animate-spin" />
            : <Sparkles className="h-3 w-3" />}
          {evaluation ? "Переоценить" : "Завершить и оценить"}
        </Button>
      </div>
    </div>
  );
}

function EvaluationBlock({ e }: { e: LeadEvaluation }) {
  const success = e.verdict === "success";
  const fields = [
    { label: "Имя", value: e.extracted_name },
    { label: "Возраст", value: e.extracted_age != null ? String(e.extracted_age) : null },
    { label: "Город", value: e.extracted_city },
    { label: "Место работы", value: e.extracted_workplace },
    { label: "Интересы", value: e.extracted_interests },
  ].filter((f) => f.value !== null && f.value !== "");

  return (
    <div className="rounded-md border border-border bg-background/40 p-3 text-[11px]">
      <div className="mb-1 flex items-center gap-1.5">
        {success
          ? <CheckCircle2 className="h-3.5 w-3.5 text-status-success" />
          : <XCircle className="h-3.5 w-3.5 text-destructive" />}
        <span className={success ? "font-semibold text-status-success" : "font-semibold text-destructive"}>
          {success ? "Завершен успешно" : (e.parse_succeeded ? "Завершен провал" : "Парсинг сломан — разобрать вручную")}
        </span>
        {e.evaluator_model && (
          <span className="mono ml-auto text-[10px] text-muted-foreground">{e.evaluator_model}</span>
        )}
      </div>
      {fields.length > 0 ? (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-2 gap-y-0.5">
          {fields.map((f) => (
            <div key={f.label} className="contents">
              <dt className="text-muted-foreground">{f.label}:</dt>
              <dd className="break-words">{f.value}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <div className="text-muted-foreground">
          Оценщик не извлёк ни одного поля из переписки.
        </div>
      )}
    </div>
  );
}

function Badge({ label, tone }: { label: string; tone: "info" | "muted" | "warn" }) {
  const toneCls =
    tone === "info"
      ? "bg-blue-500/10 text-blue-600 border-blue-500/30"
      : tone === "warn"
        ? "bg-amber-500/10 text-amber-700 border-amber-500/30"
        : "bg-muted text-muted-foreground border-border";
  return (
    <span className={`inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] uppercase tracking-wider ${toneCls}`}>
      {label}
    </span>
  );
}
