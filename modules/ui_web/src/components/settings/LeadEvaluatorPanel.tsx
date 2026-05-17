import { useEffect, useState } from "react";
import { Loader2, Save } from "lucide-react";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { ErrorBox } from "@/components/common/ErrorBox";

// 10.11 — Настройки оценщика лидов.
// Содержит:
//   • редактор файла prompts/lead_evaluator_system.md (GET/PUT /lead-evaluator/prompt)
//   • четыре числовых поля настроек (lead_evaluator.*) — readwrite через /system/settings

const TIMER_KEYS = [
  {
    key: "lead_evaluator.silence_to_obryv_hours",
    label: "Часов тишины до перехода в «Обрыв»",
    hint: "После последнего входящего сообщения от лида. Дефолт: 4",
  },
  {
    key: "lead_evaluator.silence_to_close_hours",
    label: "Часов тишины до закрытия (Завершен провал, no_response)",
    hint: "От первого контакта или обрыва. Дефолт: 48",
  },
  {
    key: "lead_evaluator.silence_check_interval_sec",
    label: "Интервал тика сторожа тишины (сек)",
    hint: "Как часто проверяет диалоги. Дефолт: 300",
  },
  {
    key: "lead_evaluator.parse_retries",
    label: "Ретраи парсинга LLM-ответа",
    hint: "Сколько раз пытаемся снова если <verdict> не найден. Дефолт: 1",
  },
];

export function LeadEvaluatorPanel() {
  const toast = useToast();
  const [prompt, setPrompt] = useState("");
  const [savedPrompt, setSavedPrompt] = useState("");
  const [path, setPath] = useState<string | null>(null);
  const [timerValues, setTimerValues] = useState<Record<string, string>>({});
  const [savedTimerValues, setSavedTimerValues] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [savingPrompt, setSavingPrompt] = useState(false);
  const [savingTimers, setSavingTimers] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    Promise.all([
      api.getEvaluatorPrompt(),
      api.listSettings("lead_evaluator."),
    ])
      .then(([p, s]) => {
        if (cancelled) return;
        setPrompt(p.content);
        setSavedPrompt(p.content);
        setPath(p.path);
        const tv: Record<string, string> = {};
        s.items.forEach((it) => { tv[it.key] = it.value; });
        setTimerValues(tv);
        setSavedTimerValues(tv);
      })
      .catch((e) => {
        if (!cancelled) {
          const d = describeApiError(e);
          setLoadError(`${d.title}${d.detail ? ` (${d.detail})` : ""}`);
        }
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const promptDirty = prompt !== savedPrompt;
  const timersDirty = TIMER_KEYS.some((k) => timerValues[k.key] !== savedTimerValues[k.key]);

  async function savePrompt() {
    setSavingPrompt(true);
    try {
      await api.saveEvaluatorPrompt(prompt);
      setSavedPrompt(prompt);
      toast.success("Промт оценщика сохранён");
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Сохранение: ${d.title}`, d.detail);
    } finally {
      setSavingPrompt(false);
    }
  }

  async function saveTimers() {
    const updates: Record<string, string> = {};
    TIMER_KEYS.forEach((k) => {
      if (timerValues[k.key] !== savedTimerValues[k.key]) {
        updates[k.key] = timerValues[k.key];
      }
    });
    if (Object.keys(updates).length === 0) return;
    setSavingTimers(true);
    try {
      const res = await api.patchSettings(updates);
      setSavedTimerValues({ ...savedTimerValues, ...updates });
      toast.success("Настройки оценщика сохранены", `Обновлено: ${res.updated}`);
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Сохранение: ${d.title}`, d.detail);
    } finally {
      setSavingTimers(false);
    }
  }

  return (
    <div className="flex flex-col gap-6 p-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold">Оценщик лидов</h1>
        <p className="text-xs text-muted-foreground">
          LLM-судья, который при закрытии диалога выдаёт вердикт
          (success/archive) и извлекает имя/возраст/город/работу/интересы.
          Срабатывает на <span className="mono">autochat.finished_by_llm</span>{" "}
          или по кнопке "Завершить и оценить" в карточке диалога.
        </p>
      </header>

      {loadError && <ErrorBox title="Не удалось загрузить настройки" detail={loadError} />}
      {loading && <div className="text-xs text-muted-foreground">загрузка…</div>}

      {!loading && !loadError && (
        <>
          {/* Промт-файл */}
          <section className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <Label className="text-sm font-semibold">Промт-файл</Label>
              {path && <span className="mono text-[10px] text-muted-foreground">{path}</span>}
            </div>
            <Textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={20}
              className="mono text-[12px]"
              placeholder="Системный промт оценщика..."
            />
            <div className="flex items-center justify-end gap-2">
              <span className="text-[11px] text-muted-foreground">
                Плейсхолдеры: {"{current_time}, {conversation_history}, {partner_username}, "}
                {"{partner_name}, {partner_bio}, {partner_birthday}, {partner_gender}"}
              </span>
              <Button
                onClick={savePrompt}
                disabled={!promptDirty || savingPrompt}
                size="sm"
              >
                {savingPrompt
                  ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                  : <Save className="mr-1.5 h-3.5 w-3.5" />}
                Сохранить промт
              </Button>
            </div>
          </section>

          {/* Числовые настройки */}
          <section className="flex flex-col gap-3 rounded-md border border-border bg-card p-4">
            <div className="text-sm font-semibold">Таймеры и поведение</div>
            {TIMER_KEYS.map((k) => (
              <div key={k.key} className="grid grid-cols-[1fr_120px] items-start gap-3">
                <div>
                  <div className="text-xs font-medium">{k.label}</div>
                  <div className="text-[10px] text-muted-foreground">{k.hint}</div>
                  <div className="mono text-[10px] text-muted-foreground/60">{k.key}</div>
                </div>
                <Input
                  type="number"
                  min={0}
                  value={timerValues[k.key] ?? ""}
                  onChange={(e) =>
                    setTimerValues({ ...timerValues, [k.key]: e.target.value })
                  }
                  className="h-8 mono text-xs"
                />
              </div>
            ))}
            <div className="flex justify-end pt-2">
              <Button
                onClick={saveTimers}
                disabled={!timersDirty || savingTimers}
                size="sm"
              >
                {savingTimers
                  ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                  : <Save className="mr-1.5 h-3.5 w-3.5" />}
                Сохранить настройки
              </Button>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
