import { useEffect, useState } from "react";
import { Loader2, Save, RefreshCw } from "lucide-react";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { ErrorBox } from "@/components/common/ErrorBox";

// 10.11 — Настройки партнёрского CRM-модуля.
// Содержит:
//   • статус подключения + размер пула (live)
//   • CRM preferences (gender_filter, geo_filter, max_concurrent_leads) — read-only
//   • три ключа из settings (poll_interval, threshold, batch_size) — readwrite

const SETTING_KEYS = [
  {
    key: "partner_crm.poll_interval_sec",
    label: "Интервал тика сторожа пула (сек)",
    hint: "Как часто проверяем размер пула. Дефолт: 60",
  },
  {
    key: "partner_crm.pool_low_threshold",
    label: "Минимальный размер пула",
    hint: "Если упало ниже — дозаливаем из CRM. Дефолт: 10",
  },
  {
    key: "partner_crm.refill_batch_size",
    label: "Сколько лидов забирать за один request-new",
    hint: "Дефолт: 50",
  },
];

type Health = { configured: boolean; pool_size: number };
type Preferences = {
  ready_to_receive?: boolean;
  max_concurrent_leads?: number;
  gender_filter?: string;
  geo_filter?: string[];
  updated_at?: string;
};

export function PartnerCrmPanel() {
  const toast = useToast();
  const [health, setHealth] = useState<Health | null>(null);
  const [prefs, setPrefs] = useState<Preferences | null>(null);
  const [prefsError, setPrefsError] = useState<string | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [savedValues, setSavedValues] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  async function reloadHealth() {
    try {
      setHealth(await api.partnerCrmHealth());
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Не удалось получить статус CRM: ${d.title}`, d.detail);
    }
  }

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([
      api.partnerCrmHealth(),
      api.listSettings("partner_crm."),
    ]).then(async ([h, s]) => {
      if (cancelled) return;
      setHealth(h);
      const tv: Record<string, string> = {};
      s.items.forEach((it) => { tv[it.key] = it.value; });
      setValues(tv);
      setSavedValues(tv);
      try {
        // partnerCrmPreferences — отдельный live-запрос в CRM
        const p = await fetch("/partner-crm/preferences");
        if (p.ok) {
          setPrefs(await p.json());
        } else {
          setPrefs(null);
          setPrefsError(`CRM вернул ${p.status}`);
        }
      } catch (e) {
        setPrefs(null);
        setPrefsError(String(e));
      }
    }).catch((e) => {
      if (!cancelled) {
        const d = describeApiError(e);
        toast.error(`Не удалось загрузить: ${d.title}`, d.detail);
      }
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const dirty = SETTING_KEYS.some((k) => values[k.key] !== savedValues[k.key]);

  async function save() {
    const updates: Record<string, string> = {};
    SETTING_KEYS.forEach((k) => {
      if (values[k.key] !== savedValues[k.key]) updates[k.key] = values[k.key];
    });
    if (Object.keys(updates).length === 0) return;
    setSaving(true);
    try {
      const res = await api.patchSettings(updates);
      setSavedValues({ ...savedValues, ...updates });
      toast.success("Настройки CRM сохранены", `Обновлено: ${res.updated}`);
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Сохранение: ${d.title}`, d.detail);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-6 p-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold">Партнёрский CRM</h1>
        <p className="text-xs text-muted-foreground">
          Подключение к L1 API партнёра, локальный пул лидов и параметры
          автозабора. Полное описание API:{" "}
          <span className="mono">docs/INT_CRM_API_TOKEN_GUIDE.md</span>.
        </p>
      </header>

      {loading && <div className="text-xs text-muted-foreground">загрузка…</div>}

      {!loading && (
        <>
          {/* Статус подключения */}
          <section className="flex flex-col gap-2 rounded-md border border-border bg-card p-4">
            <div className="flex items-center justify-between">
              <div className="text-sm font-semibold">Подключение</div>
              <Button variant="outline" size="sm" onClick={reloadHealth}>
                <RefreshCw className="h-3 w-3" />
              </Button>
            </div>
            <div className="grid grid-cols-2 gap-2 text-[11px]">
              <div>
                <span className="text-muted-foreground">Статус: </span>
                <span className={health?.configured ? "text-status-success font-semibold" : "text-destructive font-semibold"}>
                  {health?.configured ? "подключён" : "не настроен (.env)"}
                </span>
              </div>
              <div>
                <span className="text-muted-foreground">В пуле: </span>
                <span className="mono">{health?.pool_size ?? "?"}</span>
              </div>
            </div>
          </section>

          {/* CRM preferences (read-only) */}
          {prefs && (
            <section className="flex flex-col gap-2 rounded-md border border-border bg-card p-4">
              <div className="text-sm font-semibold">CRM preferences (read-only)</div>
              <div className="text-[10px] text-muted-foreground">
                Эти значения настраиваются на стороне партнёра (через их админку).
                Меняются через CRM API, не через нас.
              </div>
              <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-[11px]">
                <dt className="text-muted-foreground">ready_to_receive:</dt>
                <dd>{prefs.ready_to_receive ? "true" : "false"}</dd>
                <dt className="text-muted-foreground">max_concurrent_leads:</dt>
                <dd className="mono">{prefs.max_concurrent_leads}</dd>
                <dt className="text-muted-foreground">gender_filter:</dt>
                <dd className="mono">{prefs.gender_filter ?? "—"}</dd>
                <dt className="text-muted-foreground">geo_filter:</dt>
                <dd className="mono">{(prefs.geo_filter ?? []).join(", ") || "—"}</dd>
              </dl>
            </section>
          )}
          {!prefs && prefsError && (
            <ErrorBox title="CRM preferences недоступны" detail={prefsError} />
          )}

          {/* Числовые настройки */}
          <section className="flex flex-col gap-3 rounded-md border border-border bg-card p-4">
            <div className="text-sm font-semibold">Параметры автозабора</div>
            {SETTING_KEYS.map((k) => (
              <div key={k.key} className="grid grid-cols-[1fr_120px] items-start gap-3">
                <div>
                  <div className="text-xs font-medium">{k.label}</div>
                  <div className="text-[10px] text-muted-foreground">{k.hint}</div>
                  <div className="mono text-[10px] text-muted-foreground/60">{k.key}</div>
                </div>
                <Input
                  type="number"
                  min={0}
                  value={values[k.key] ?? ""}
                  onChange={(e) => setValues({ ...values, [k.key]: e.target.value })}
                  className="h-8 mono text-xs"
                />
              </div>
            ))}
            <div className="flex justify-end pt-2">
              <Button onClick={save} disabled={!dirty || saving} size="sm">
                {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Save className="mr-1.5 h-3.5 w-3.5" />}
                Сохранить
              </Button>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
