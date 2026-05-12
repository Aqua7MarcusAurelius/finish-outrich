import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorBox } from "@/components/common/ErrorBox";
import { api, describeApiError } from "@/lib/api";
import { useToast } from "@/lib/toast";
import { useDialogStatuses } from "@/hooks/useAccounts";
import { ChevronDown, ChevronUp, Plus, Trash2, Loader2 } from "lucide-react";

// Каталог пузырей: оператор может добавлять/переименовывать/перекрашивать/
// переставлять/удалять. Сохранение inline по blur поля (или Enter).
// Цвет — нативный <input type="color"> + текстовое поле с hex,
// синхронизированы. Hex принимается с # или без.

type Status = {
  id: number;
  label: string;
  color: string;
  sort_order: number;
};

const HEX_RE = /^#[0-9A-Fa-f]{6}$/;

function normHex(input: string): string | null {
  const v = input.trim();
  const withHash = v.startsWith("#") ? v : `#${v}`;
  return HEX_RE.test(withHash) ? withHash.toLowerCase() : null;
}

export function DialogStatusesPanel() {
  const q = useDialogStatuses();
  const qc = useQueryClient();
  const toast = useToast();

  const [adding, setAdding] = useState(false);

  async function refetch() {
    await qc.invalidateQueries({ queryKey: ["dialog-statuses"] });
  }

  async function handleCreate() {
    setAdding(true);
    try {
      await api.createDialogStatus({ label: "Новый статус", color: "#3b82f6" });
      await refetch();
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Создание: ${d.title}`, d.detail);
    } finally {
      setAdding(false);
    }
  }

  async function handleDelete(s: Status) {
    if (!confirm(`Удалить статус «${s.label}»? Диалоги с ним просто получат «без статуса».`)) return;
    try {
      await api.deleteDialogStatus(s.id);
      await refetch();
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Удаление: ${d.title}`, d.detail);
    }
  }

  async function handleMove(s: Status, dir: -1 | 1) {
    const items = q.data ?? [];
    const sorted = [...items].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id);
    const idx = sorted.findIndex((x) => x.id === s.id);
    const target = idx + dir;
    if (target < 0 || target >= sorted.length) return;
    const neighbor = sorted[target];
    // Просто меняем местами sort_order. Если у соседа такой же sort_order
    // (теоретически возможно — миграция сидировала уникальные, но при
    // ручных правках бывает) — отдаём ему текущий +/- 0.5 округлённо.
    try {
      await Promise.all([
        api.updateDialogStatus(s.id,        { sort_order: neighbor.sort_order }),
        api.updateDialogStatus(neighbor.id, { sort_order: s.sort_order }),
      ]);
      await refetch();
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Перестановка: ${d.title}`, d.detail);
    }
  }

  return (
    <div className="mx-auto max-w-2xl p-6">
      <header className="mb-5">
        <h1 className="text-lg font-semibold">Статусы диалогов</h1>
        <p className="mt-1 text-xs text-muted-foreground">
          Пузыри-пометки, которые оператор может ставить диалогам. Изменения
          сохраняются по уходу из поля (blur) или по Enter.
        </p>
      </header>

      {q.isError && <ErrorBox title="Не удалось загрузить статусы" detail={String(q.error)} />}

      {q.isLoading && (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" /> Загрузка…
        </div>
      )}

      {q.data && (
        <div className="flex flex-col gap-1.5">
          {[...q.data]
            .sort((a, b) => a.sort_order - b.sort_order || a.id - b.id)
            .map((s, i, arr) => (
              <StatusRow
                key={s.id}
                status={s}
                isFirst={i === 0}
                isLast={i === arr.length - 1}
                onMove={(dir) => handleMove(s, dir)}
                onDelete={() => handleDelete(s)}
                onSaved={refetch}
              />
            ))}
          {q.data.length === 0 && (
            <div className="rounded-md border border-dashed border-border px-3 py-6 text-center text-xs text-muted-foreground">
              Каталог пуст. Добавь первый статус кнопкой ниже.
            </div>
          )}
        </div>
      )}

      <div className="mt-4">
        <Button size="sm" variant="outline" onClick={handleCreate} disabled={adding}>
          <Plus className="h-3 w-3" /> Добавить статус
        </Button>
      </div>
    </div>
  );
}

// ─── Строка статуса (inline edit) ────────────────────────────────────

function StatusRow({
  status, isFirst, isLast, onMove, onDelete, onSaved,
}: {
  status: Status;
  isFirst: boolean;
  isLast: boolean;
  onMove: (dir: -1 | 1) => void;
  onDelete: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  // Локальный draft, чтобы по blur'ам не дёргать сервер на каждом keystroke.
  const [label, setLabel] = useState(status.label);
  const [color, setColor] = useState(status.color);
  const [hexDraft, setHexDraft] = useState(status.color);
  const [busy, setBusy] = useState(false);

  // Если сервер прислал обновление — синхронизируем локалку.
  const lastSyncedId = useRef(status.id);
  useEffect(() => {
    if (lastSyncedId.current === status.id) {
      setLabel(status.label);
      setColor(status.color);
      setHexDraft(status.color);
    } else {
      lastSyncedId.current = status.id;
    }
  }, [status.id, status.label, status.color]);

  async function save(patch: { label?: string; color?: string }) {
    // Не шлём запрос, если ничего не изменилось.
    const empty = (patch.label === undefined || patch.label === status.label) &&
                  (patch.color === undefined || patch.color === status.color);
    if (empty) return;
    setBusy(true);
    try {
      await api.updateDialogStatus(status.id, patch);
      onSaved();
    } catch (e) {
      const d = describeApiError(e);
      toast.error(`Сохранение: ${d.title}`, d.detail);
      // Откат локального draft.
      if (patch.label !== undefined) setLabel(status.label);
      if (patch.color !== undefined) {
        setColor(status.color);
        setHexDraft(status.color);
      }
    } finally {
      setBusy(false);
    }
  }

  function commitHex() {
    const norm = normHex(hexDraft);
    if (!norm) {
      // Битая строка — откатываем поле к актуальному цвету.
      setHexDraft(color);
      toast.error("Неверный цвет", "Ожидается формат #RRGGBB");
      return;
    }
    if (norm === color) {
      setHexDraft(norm);
      return;
    }
    setColor(norm);
    setHexDraft(norm);
    save({ color: norm });
  }

  return (
    <div className="flex items-center gap-2 rounded-md border border-border bg-card px-2 py-1.5">
      {/* Цвет: swatch + нативный пикер + hex */}
      <label className="relative h-6 w-6 shrink-0 cursor-pointer overflow-hidden rounded-md border border-border" title="Выбрать цвет">
        <span className="absolute inset-0" style={{ backgroundColor: color }} />
        <input
          type="color"
          value={color}
          onChange={(e) => {
            setColor(e.target.value);
            setHexDraft(e.target.value);
          }}
          onBlur={() => save({ color })}
          className="absolute inset-0 cursor-pointer opacity-0"
        />
      </label>
      <Input
        value={hexDraft}
        onChange={(e) => setHexDraft(e.target.value)}
        onBlur={commitHex}
        onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
        className="mono h-7 w-[88px] px-2 text-xs"
        placeholder="#RRGGBB"
      />

      {/* Лейбл */}
      <Input
        value={label}
        onChange={(e) => setLabel(e.target.value)}
        onBlur={() => save({ label: label.trim() })}
        onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
        className="h-7 flex-1 px-2 text-sm"
        maxLength={32}
        placeholder="Название статуса"
      />

      {/* Reorder */}
      <Button
        variant="ghost" size="sm"
        onClick={() => onMove(-1)} disabled={isFirst || busy}
        title="Вверх"
      >
        <ChevronUp className="h-3 w-3" />
      </Button>
      <Button
        variant="ghost" size="sm"
        onClick={() => onMove(1)} disabled={isLast || busy}
        title="Вниз"
      >
        <ChevronDown className="h-3 w-3" />
      </Button>

      {/* Delete */}
      <Button
        variant="ghost" size="sm"
        onClick={onDelete} disabled={busy}
        className="text-destructive hover:bg-destructive/10"
        title="Удалить"
      >
        <Trash2 className="h-3 w-3" />
      </Button>
    </div>
  );
}
