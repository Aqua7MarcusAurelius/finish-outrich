import { useEffect, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { ArrowLeft, Eye, Copy, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ErrorBox } from "@/components/common/ErrorBox";
import { PromptPreviewDialog } from "@/components/dialogs/PromptPreviewDialog";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";
import { useAccounts } from "@/hooks/useAccounts";

// Свободный редактор per-worker промтов. Два текста (initial + reply),
// плейсхолдеры подставляются на бэке во время генерации (см.
// modules/autochat/generation.py). Превью без вызова LLM.

type Placeholder = {
  name: string;
  description: string;
  scope: "both" | "reply-only";
};

const PLACEHOLDERS: Placeholder[] = [
  { name: "current_time",        description: "Текущее UTC-время (DD.MM.YYYY HH:MM:SS)", scope: "both" },
  { name: "worker_name",         description: "Имя нашего воркера (как в карточке)",     scope: "both" },
  { name: "partner_username",    description: "@username собеседника, без @",            scope: "both" },
  { name: "partner_name",        description: "Имя собеседника (first + last)",          scope: "both" },
  { name: "partner_bio",         description: "Telegram-bio собеседника",                scope: "both" },
  { name: "conversation_history",description: "Вся история переписки в формате блоков (см. docs/history_format_spec.md)", scope: "reply-only" },
  { name: "messages_count",      description: "Сколько сообщений в диалоге",             scope: "reply-only" },
  { name: "days_since_first",    description: "Сколько дней с первого сообщения",        scope: "reply-only" },
];

export function WorkerPromptPage() {
  const params = useParams<{ accountId: string }>();
  const accountId = params.accountId ? Number(params.accountId) : null;
  const navigate = useNavigate();
  const toast = useToast();
  const accountsQ = useAccounts();
  const account = accountId != null
    ? accountsQ.data?.find((a) => a.id === accountId)
    : undefined;

  const [initialTemplate, setInitialTemplate] = useState("");
  const [replyTemplate, setReplyTemplate] = useState("");
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  useEffect(() => {
    if (accountId == null) return;
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    api.getWorkerPrompts(accountId)
      .then((p) => {
        if (cancelled) return;
        setInitialTemplate(p.initial_template);
        setReplyTemplate(p.reply_template);
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

  async function save() {
    if (accountId == null) return;
    setSaving(true);
    try {
      const saved = await api.saveWorkerPrompts(accountId, {
        initial_template: initialTemplate,
        reply_template: replyTemplate,
      });
      setUpdatedAt(saved.updated_at);
      toast.success("Промт сохранён", account?.name || `account_${accountId}`);
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Сохранение: ${d.title}`, d.detail);
    } finally {
      setSaving(false);
    }
  }

  async function copyPlaceholder(name: string) {
    const literal = `{${name}}`;
    try {
      await navigator.clipboard.writeText(literal);
      setCopiedKey(name);
      setTimeout(() => setCopiedKey((k) => (k === name ? null : k)), 1200);
    } catch {
      toast.error("Не удалось скопировать", "браузер заблокировал доступ к буферу");
    }
  }

  if (accountId == null) {
    return <div className="p-4 text-sm text-muted-foreground">Не указан accountId</div>;
  }

  return (
    <div className="flex h-full flex-col">
      {/* Шапка */}
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
        <div className="mx-auto max-w-3xl px-4 py-4 pb-24">
          {loading && <div className="text-xs text-muted-foreground">загрузка…</div>}
          {loadError && <ErrorBox title="Не удалось загрузить промт" detail={loadError} />}

          {!loading && !loadError && (
            <div className="flex flex-col gap-6">
              {/* Справочник плейсхолдеров */}
              <PlaceholderReference copiedKey={copiedKey} onCopy={copyPlaceholder} />

              {/* Initial template */}
              <PromptBlock
                title="Initial — первое сообщение"
                description={
                  <>
                    Этот текст уходит в LLM как <span className="mono">system</span>-промт
                    при запуске нового авто-диалога ("+ Новый авто-диалог"). Истории
                    ещё нет — задача LLM написать <b>одно</b> первое сообщение,
                    без <span className="mono">&lt;msg&gt;</span>-тегов.
                    Доступны общие плейсхолдеры (см. справочник выше).
                    <br />
                    <span className="text-destructive">Без него</span> "+ Новый авто-диалог"
                    выдаст ошибку и сообщение не уйдёт.
                  </>
                }
                value={initialTemplate}
                onChange={setInitialTemplate}
                rows={12}
                placeholder="Например: Ты — Маша. Напиши первое сообщение @{partner_username}, как будто разгребаешь рабочую телегу..."
              />

              {/* Reply template */}
              <PromptBlock
                title="Reply — ответы в активной переписке"
                description={
                  <>
                    Этот текст уходит в LLM как <span className="mono">system</span>-промт
                    каждый раз когда нужно сгенерить ответ. Здесь работают
                    <b> все</b> плейсхолдеры — включая
                    <span className="mono"> {"{conversation_history}"}</span>,
                    <span className="mono"> {"{messages_count}"}</span> и
                    <span className="mono"> {"{days_since_first}"}</span>.
                    <br />
                    Не забудь добавить инструкцию про
                    <span className="mono"> &lt;msg&gt;...&lt;/msg&gt;</span> для
                    сегментации, и про <span className="mono">&lt;finishdialog/&gt;</span>
                    {" "}если хочешь чтобы LLM могла сама завершить разговор.
                    <br />
                    <span className="text-destructive">Без него</span> автодиалог не
                    сгенерит ни одного ответа (Event Log:{" "}
                    <span className="mono">autochat.generation_skipped</span>).
                  </>
                }
                value={replyTemplate}
                onChange={setReplyTemplate}
                rows={24}
                placeholder="Например: Ты — Маша. {worker_name} ведёт диалог с {partner_name} (@{partner_username})..."
              />
            </div>
          )}
        </div>
      </div>

      {/* Sticky футер */}
      {!loading && !loadError && (
        <div className="flex h-14 shrink-0 items-center justify-end gap-2 border-t border-border px-4">
          <Button variant="outline" size="sm" onClick={() => setPreviewOpen(true)} disabled={saving} title="Превью того, что уйдёт в LLM (без вызова модели)">
            <Eye className="h-3 w-3" /> Что уйдёт в LLM
          </Button>
          <div className="flex-1" />
          <Button variant="ghost" size="sm" onClick={() => navigate(`/dialogs/${accountId}`)} disabled={saving}>
            Отмена
          </Button>
          <Button size="sm" onClick={save} disabled={saving}>
            {saving ? "Сохраняем…" : "Сохранить"}
          </Button>
        </div>
      )}

      {accountId != null && (
        <PromptPreviewDialog
          open={previewOpen}
          accountId={accountId}
          initialTemplate={initialTemplate}
          replyTemplate={replyTemplate}
          onClose={() => setPreviewOpen(false)}
        />
      )}
    </div>
  );
}

function PlaceholderReference({
  copiedKey, onCopy,
}: {
  copiedKey: string | null;
  onCopy: (name: string) => void;
}) {
  const both = PLACEHOLDERS.filter((p) => p.scope === "both");
  const replyOnly = PLACEHOLDERS.filter((p) => p.scope === "reply-only");
  return (
    <div className="rounded-md bg-muted/30 p-3 hairline border-border">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Доступные плейсхолдеры
      </div>
      <div className="text-[11px] text-muted-foreground mb-2">
        Кликни иконку справа чтобы скопировать <span className="mono">{"{name}"}</span> в буфер.
        Всё что внутри <span className="mono">&lt;!-- ... --&gt;</span> вырезается перед отправкой
        в LLM — можно писать заметки прямо в шаблоне.
      </div>
      <div className="flex flex-col gap-0.5">
        {both.map((p) => (
          <PlaceholderRow key={p.name} p={p} copied={copiedKey === p.name} onCopy={onCopy} />
        ))}
      </div>
      <div className="mt-2 mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        Только в reply-промте (требуют существующего диалога):
      </div>
      <div className="flex flex-col gap-0.5">
        {replyOnly.map((p) => (
          <PlaceholderRow key={p.name} p={p} copied={copiedKey === p.name} onCopy={onCopy} />
        ))}
      </div>
    </div>
  );
}

function PlaceholderRow({
  p, copied, onCopy,
}: {
  p: Placeholder;
  copied: boolean;
  onCopy: (name: string) => void;
}) {
  return (
    <div className="flex items-center gap-2 rounded px-1.5 py-1 hover:bg-accent/40">
      <code className="mono text-xs text-foreground">{`{${p.name}}`}</code>
      <span className="flex-1 text-[11px] text-muted-foreground">— {p.description}</span>
      <button
        type="button"
        onClick={() => onCopy(p.name)}
        title="Скопировать в буфер"
        className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-background hover:text-foreground"
      >
        {copied ? <Check className="h-3 w-3 text-status-success" /> : <Copy className="h-3 w-3" />}
      </button>
    </div>
  );
}

function PromptBlock({
  title, description, value, onChange, rows, placeholder,
}: {
  title: string;
  description: React.ReactNode;
  value: string;
  onChange: (v: string) => void;
  rows: number;
  placeholder: string;
}) {
  const empty = value.trim().length === 0;
  return (
    <div className="flex flex-col gap-1.5">
      <Label className="text-sm font-semibold">{title}</Label>
      <div className="text-[11px] text-muted-foreground leading-relaxed">{description}</div>
      <Textarea
        rows={rows}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="mono text-xs"
        spellCheck={false}
      />
      {empty && (
        <div className="text-[10px] text-destructive">
          поле пустое
        </div>
      )}
    </div>
  );
}
