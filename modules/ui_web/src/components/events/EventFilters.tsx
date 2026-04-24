import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { EventFilters } from "@/types/api";
import { X } from "lucide-react";

const MODULES: (EventFilters["module"])[] = [
  "telegram", "history", "transcription", "description", "auth", "worker_manager", "autochat",
];

const STATUSES: (EventFilters["status"])[] = ["success", "error", "in_progress"];

const RANGES = [
  { label: "1 час",  value: "1h" },
  { label: "Сегодня", value: "today" },
  { label: "24 часа", value: "24h" },
  { label: "7 дней", value: "7d" },
];

export function EventFiltersBar({
  value,
  onChange,
  range,
  onRangeChange,
}: {
  value: EventFilters;
  onChange: (patch: Partial<EventFilters>) => void;
  range: string;
  onRangeChange: (r: string) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Input
        className="w-36"
        placeholder="account id"
        value={value.account ?? ""}
        onChange={(e) => onChange({ account: e.target.value ? Number(e.target.value) : undefined })}
      />

      <Select value={value.module ?? "__all"} onValueChange={(v) => onChange({ module: v === "__all" ? undefined : (v as EventFilters["module"]) })}>
        <SelectTrigger className="w-40"><SelectValue placeholder="Module" /></SelectTrigger>
        <SelectContent>
          <SelectItem value="__all">все модули</SelectItem>
          {MODULES.map((m) => <SelectItem key={m} value={m!}>{m}</SelectItem>)}
        </SelectContent>
      </Select>

      <Input
        className="w-52"
        placeholder="event type или type.*"
        value={value.type ?? ""}
        onChange={(e) => onChange({ type: e.target.value || undefined })}
      />

      <Select value={value.status ?? "__all"} onValueChange={(v) => onChange({ status: v === "__all" ? undefined : (v as EventFilters["status"]) })}>
        <SelectTrigger className="w-36"><SelectValue placeholder="Status" /></SelectTrigger>
        <SelectContent>
          <SelectItem value="__all">любой</SelectItem>
          {STATUSES.map((s) => <SelectItem key={s} value={s!}>{s}</SelectItem>)}
        </SelectContent>
      </Select>

      <Select value={range} onValueChange={onRangeChange}>
        <SelectTrigger className="w-32"><SelectValue placeholder="Range" /></SelectTrigger>
        <SelectContent>
          {RANGES.map((r) => <SelectItem key={r.value} value={r.value}>{r.label}</SelectItem>)}
        </SelectContent>
      </Select>

      <Button variant="ghost" size="sm" onClick={() => { onChange({ account: undefined, module: undefined, type: undefined, status: undefined }); onRangeChange("1h"); }}>
        <X className="h-3 w-3" /> Clear
      </Button>
    </div>
  );
}
