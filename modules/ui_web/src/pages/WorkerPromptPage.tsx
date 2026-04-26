import { useEffect, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ErrorBox } from "@/components/common/ErrorBox";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";
import { useAccounts } from "@/hooks/useAccounts";
import type { WorkerPrompts } from "@/types/api";

// Структурированный редактор per-worker промта (AutoChat).
// 8 семантических полей для reply + одно initial_system.
// Бэк собирает финальный system-промт из непустых reply-полей с
// заголовками секций (см. modules/autochat/prompts.py).
//
// Все 8 пустые → autochat.generation_skipped в Event Log, ответы
// не идут пока хотя бы одно поле не заполнено.

type FieldKey =
  | "fabula" | "bio" | "style" | "forbidden"
  | "length_hint" | "goals" | "format_reply" | "examples"
  | "initial_system";

type FieldDef = {
  key: FieldKey;
  label: string;
  hint: string;
  rows: number;
};

const REPLY_FIELDS: FieldDef[] = [
  { key: "fabula",       label: "Фабула запроса",          hint: "Шапка: что это и зачем. Объясняет нейросети контекст её задачи.", rows: 4 },
  { key: "bio",          label: "Био",                     hint: "Биография персонажа от которого ведётся диалог.", rows: 8 },
  { key: "style",        label: "Стиль",                   hint: "Как именно общается персонаж (тон, длина фраз, эмодзи и т.п.).", rows: 6 },
  { key: "forbidden",    label: "Запреты",                 hint: "Чего персонаж не должен делать ни при каких обстоятельствах.", rows: 6 },
  { key: "length_hint",  label: "Длина разговора",         hint: "На сколько ходов рассчитывать темп — короткий знакомочный или долгий.", rows: 3 },
  { key: "goals",        label: "Цели",                    hint: "Какие вопросы / темы нужно обсудить за разговор.", rows: 5 },
  { key: "format_reply", label: "Форматирование ответа",   hint: "Жёсткая инструкция о формате — наш парсер опирается на <msg>-теги.", rows: 6 },
  { key: "examples",     label: "Примеры",                 hint: "Few-shot-примеры удачных приёмов в общении. Самый сильный рычаг качества.", rows: 8 },
];

const INITIAL_FIELD: FieldDef = {
  key: "initial_system",
  label: "Initial (первое сообщение)",
  hint: "Промт для ПЕРВОГО сообщения новому собеседнику. Один баббл, без <msg>-тегов.",
  rows: 8,
};

const EMPTY_FORM: Record<FieldKey, string> = {
  fabula: "", bio: "", style: "", forbidden: "",
  length_hint: "", goals: "", format_reply: "", examples: "",
  initial_system: "",
};

export function WorkerPromptPage() {
  const params = useParams<{ accountId: string }>();
  const accountId = params.accountId ? Number(params.accountId) : null;
  const navigate = useNavigate();
  const toast = useToast();
  const accountsQ = useAccounts();
  const account = accountId != null
    ? accountsQ.data?.find((a) => a.id === accountId)
    : undefined;

  const [form, setForm] = useState<Record<FieldKey, string>>(EMPTY_FORM);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (accountId == null) return;
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    api.getWorkerPrompts(accountId)
      .then((p) => {
        if (cancelled) return;
        setForm({
          fabula: p.fabula,
          bio: p.bio,
          style: p.style,
          forbidden: p.forbidden,
          length_hint: p.length_hint,
          goals: p.goals,
          format_reply: p.format_reply,
          examples: p.examples,
          initial_system: p.initial_system,
        });
        setUpdatedAt(p.updated_at);
      })
      .catch((e) => {
        if (cancelled) return;
        const d = describeApiError(e);
        setLoadError(`${d.title}${d.detail ? ` (${d.detail})` : ""}`);
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [accountId]);

  function updateField(key: FieldKey, value: string) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function save() {
    if (accountId == null) return;
    setSaving(true);
    try {
      const body: Omit<WorkerPrompts, "account_id" | "updated_at"> = {
        fabula: form.fabula,
        bio: form.bio,
        style: form.style,
        forbidden: form.forbidden,
        length_hint: form.length_hint,
        goals: form.goals,
        format_reply: form.format_reply,
        examples: form.examples,
        initial_system: form.initial_system,
      };
      const saved = await api.saveWorkerPrompts(accountId, body);
      setUpdatedAt(saved.updated_at);
      toast.success("Промт сохранён", account?.name || `account_${accountId}`);
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Сохранение: ${d.title}`, d.detail);
    } finally {
      setSaving(false);
    }
  }

  const allReplyEmpty = REPLY_FIELDS.every((f) => form[f.key].trim() === "");
  const formatHasMsgTag = form.format_reply.includes("<msg>");
  const formatWarn = form.format_reply.trim().length > 0 && !formatHasMsgTag;

  if (accountId == null) {
    return <div className="p-4 text-sm text-muted-foreground">Не указан accountId</div>;
  }

  return (
    <div className="flex h-full flex-col">
      {/* Шапка страницы — высота под общую сетку (h-12 как у других страниц) */}
      <div className="flex h-12 shrink-0 items-center gap-3 border-b border-border px-3">
        <Link
          to={`/dialogs/${accountId}`}
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> к диалогам
        </Link>
        <div className="text-sm font-semibold">
          Промт воркера: {account?.name || `account_${accountId}`}
        </div>
        {updatedAt && (
          <span className="mono text-[10px] text-muted-foreground">
            · обновлён {new Date(updatedAt).toLocaleString()}
          </span>
        )}
      </div>

      {/* Тело */}
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl px-4 py-4">
          {loading && <div className="text-xs text-muted-foreground">загрузка…</div>}
          {loadError && <ErrorBox title="Не удалось загрузить промт" detail={loadError} />}

          {!loading && !loadError && (
            <div className="flex flex-col gap-5 pb-24">
              {allReplyEmpty && (
                <div className="rounded-md border border-destructive/60 bg-destructive/10 p-2 text-xs text-destructive">
                  Все поля reply-промта пустые → автоответы не генерируются. Заполни хотя бы одно поле.
                </div>
              )}

              <div className="text-xs uppercase tracking-wider text-muted-foreground">
                Reply — генерация ответов в активной переписке
              </div>

              {REPLY_FIELDS.map((f) => (
                <FieldBlock
                  key={f.key}
                  def={f}
                  value={form[f.key]}
                  onChange={(v) => updateField(f.key, v)}
                  warn={f.key === "format_reply" && formatWarn ? "Не нашёл упоминание <msg> — парсер ответов опирается на эти теги." : null}
                />
              ))}

              <div className="mt-4 text-xs uppercase tracking-wider text-muted-foreground">
                Initial — первое сообщение новому собеседнику
              </div>

              <FieldBlock
                def={INITIAL_FIELD}
                value={form.initial_system}
                onChange={(v) => updateField("initial_system", v)}
                warn={null}
              />
            </div>
          )}
        </div>
      </div>

      {/* Sticky футер */}
      {!loading && !loadError && (
        <div className="flex h-14 shrink-0 items-center justify-end gap-2 border-t border-border px-4">
          <Button variant="ghost" size="sm" onClick={() => navigate(`/dialogs/${accountId}`)} disabled={saving}>
            Отмена
          </Button>
          <Button size="sm" onClick={save} disabled={saving}>
            {saving ? "Сохраняем…" : "Сохранить"}
          </Button>
        </div>
      )}
    </div>
  );
}

function FieldBlock({
  def, value, onChange, warn,
}: {
  def: FieldDef;
  value: string;
  onChange: (v: string) => void;
  warn: string | null;
}) {
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={`prompt-${def.key}`} className="text-sm font-semibold">
        {def.label}
      </Label>
      <div className="text-[11px] text-muted-foreground">{def.hint}</div>
      <Textarea
        id={`prompt-${def.key}`}
        rows={def.rows}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mono text-xs"
      />
      {warn && <div className="text-[11px] text-status-warning">{warn}</div>}
    </div>
  );
}
