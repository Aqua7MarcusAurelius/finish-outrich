import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Loader2, RefreshCw, Users } from "lucide-react";
import type { Account, LeadSource, TargetGender } from "@/types/api";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";

// Панель настроек источника лидов для одного воркера.
// Используется в WorkerPromptPage (10.9). Содержит:
//   • селект lead_source (manual | partner_crm)
//   • селект target_gender (any | female | male), активен только в CRM-режиме
//   • статус подключения партнёрского CRM + размер пула
//   • кнопка "Забрать N лидов сейчас" (ручной рефилл)

type Props = {
  account: Account;
  // Колбэк после успешного сохранения — родительская страница может
  // инвалидировать кеши useAccounts.
  onSaved?: () => void;
};

export function LeadSettingsPanel({ account, onSaved }: Props) {
  const toast = useToast();
  const qc = useQueryClient();
  const [leadSource, setLeadSource] = useState<LeadSource>(account.lead_source);
  const [targetGender, setTargetGender] = useState<TargetGender>(account.target_gender);
  const [saving, setSaving] = useState(false);
  const [health, setHealth] = useState<{ configured: boolean; pool_size: number } | null>(null);
  const [refillBusy, setRefillBusy] = useState(false);
  const [refillCount, setRefillCount] = useState(50);

  // Подтягиваем здоровье модуля при монтировании + при смене lead_source
  // на partner_crm (чтобы оператор увидел актуальный pool_size перед
  // первым включением).
  useEffect(() => {
    let cancelled = false;
    api.partnerCrmHealth()
      .then((h) => { if (!cancelled) setHealth(h); })
      .catch(() => { if (!cancelled) setHealth(null); });
    return () => { cancelled = true; };
  }, [leadSource]);

  const dirty =
    leadSource !== account.lead_source ||
    targetGender !== account.target_gender;

  async function save() {
    setSaving(true);
    try {
      await api.patchLeadSettings(account.id, {
        lead_source: leadSource,
        target_gender: targetGender,
      });
      toast.success(
        "Настройки источника сохранены",
        `${account.name}: ${describeSource(leadSource)} · ${describeGender(targetGender)}`,
      );
      qc.invalidateQueries({ queryKey: ["accounts"] });
      onSaved?.();
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Не удалось сохранить: ${d.title}`, d.detail);
    } finally {
      setSaving(false);
    }
  }

  async function refill() {
    setRefillBusy(true);
    try {
      const r = await api.partnerCrmRefill(refillCount);
      toast.success(
        "Лиды забраны",
        `+${r.inserted} в пул (получено ${r.fetched} из CRM)`,
      );
      // Обновим health на актуальный pool_size
      try {
        setHealth(await api.partnerCrmHealth());
      } catch {/* noop */}
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Не удалось забрать: ${d.title}`, d.detail);
    } finally {
      setRefillBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border bg-card p-4">
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold">Источник лидов</div>
        {leadSource === "partner_crm" && health && (
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <Users className="h-3.5 w-3.5" />
            <span>
              CRM: {health.configured ? "подключён" : "не настроен"} · пул {health.pool_size}
            </span>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <Label className="text-[11px]">Откуда брать лидов</Label>
          <select
            value={leadSource}
            onChange={(e) => setLeadSource(e.target.value as LeadSource)}
            className="h-8 rounded-md border border-border bg-background px-2 text-xs"
            disabled={saving}
          >
            <option value="manual">Ручной (своя очередь)</option>
            <option value="partner_crm">Пул партнёрской CRM</option>
          </select>
          <div className="text-[10px] text-muted-foreground">
            {leadSource === "manual"
              ? "Лиды добавляются в очередь воркера вручную через автозапуск."
              : "Воркер на тике забирает следующего лида из общего пула CRM."}
          </div>
        </div>

        <div className="flex flex-col gap-1">
          <Label className="text-[11px]">Фильтр пола (для CRM-пула)</Label>
          <select
            value={targetGender}
            onChange={(e) => setTargetGender(e.target.value as TargetGender)}
            className="h-8 rounded-md border border-border bg-background px-2 text-xs disabled:opacity-50"
            disabled={saving || leadSource !== "partner_crm"}
          >
            <option value="any">Любой</option>
            <option value="female">Женский</option>
            <option value="male">Мужской</option>
          </select>
          <div className="text-[10px] text-muted-foreground">
            {leadSource === "partner_crm"
              ? "При подборе из пула берём только подходящих или с неизвестным полом."
              : "Активен только в режиме «Пул партнёрской CRM»."}
          </div>
        </div>
      </div>

      <div className="flex items-center justify-between border-t border-border pt-3">
        <div className="flex items-center gap-2">
          {leadSource === "partner_crm" && health?.configured && (
            <>
              <input
                type="number"
                min={1}
                max={200}
                value={refillCount}
                onChange={(e) => setRefillCount(Math.max(1, Math.min(200, Number(e.target.value) || 50)))}
                className="h-8 w-16 rounded-md border border-border bg-background px-2 text-xs"
                disabled={refillBusy}
              />
              <Button
                variant="outline"
                size="sm"
                disabled={refillBusy}
                onClick={refill}
              >
                {refillBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                <span className="ml-1.5">Забрать N лидов сейчас</span>
              </Button>
            </>
          )}
        </div>
        <Button onClick={save} disabled={!dirty || saving} size="sm">
          {saving && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
          Сохранить настройки
        </Button>
      </div>
    </div>
  );
}

function describeSource(s: LeadSource): string {
  return s === "manual" ? "Ручной" : "Пул CRM";
}
function describeGender(g: TargetGender): string {
  if (g === "any") return "любой пол";
  if (g === "female") return "женский";
  return "мужской";
}
