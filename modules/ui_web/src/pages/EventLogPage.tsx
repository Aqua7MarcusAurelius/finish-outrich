import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { EventFiltersBar } from "@/components/events/EventFilters";
import { EventRow } from "@/components/events/EventRow";
import { MetricsBar } from "@/components/events/MetricsBar";
import { EventDetailDialog } from "@/components/events/EventDetailDialog";
import { ErrorBox } from "@/components/common/ErrorBox";
import { useEventList, useEventStats, useEventStream } from "@/hooks/useEvents";
import type { EventFilters } from "@/types/api";
import { api } from "@/lib/api";
import { Download } from "lucide-react";

function rangeToTimestamps(range: string): { from: string; to: string; label: string } {
  const to = new Date();
  const from = new Date(to);
  switch (range) {
    case "1h":    from.setHours(to.getHours() - 1);  return { from: from.toISOString(), to: to.toISOString(), label: "1 час" };
    case "today": from.setHours(0, 0, 0, 0);          return { from: from.toISOString(), to: to.toISOString(), label: "сегодня" };
    case "24h":   from.setDate(to.getDate() - 1);     return { from: from.toISOString(), to: to.toISOString(), label: "24 часа" };
    case "7d":    from.setDate(to.getDate() - 7);     return { from: from.toISOString(), to: to.toISOString(), label: "7 дней" };
    default:      from.setHours(to.getHours() - 1);  return { from: from.toISOString(), to: to.toISOString(), label: "1 час" };
  }
}

export function EventLogPage() {
  const [range, setRange] = useState<string>("1h");
  const [filter, setFilter] = useState<EventFilters>({});
  const [followTail, setFollowTail] = useState(true);
  const [selectedEvent, setSelectedEvent] = useState<number | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const { from, to, label } = useMemo(() => rangeToTimestamps(range), [range]);
  const effective: EventFilters = useMemo(() => ({ ...filter, from, to }), [filter, from, to]);

  const listQ  = useEventList(effective);
  const statsQ = useEventStats(effective);
  const { live } = useEventStream({
    account: filter.account, module: filter.module, type: filter.type, status: filter.status,
  });

  // Merge: newest live events on top, older archive below — deduped by id
  const merged = useMemo(() => {
    const map = new Map<number, typeof live[number]>();
    for (const e of live)                 map.set(e.id, e);
    for (const e of listQ.data?.items ?? []) if (!map.has(e.id)) map.set(e.id, e);
    return [...map.values()].sort((a, b) => b.id - a.id);
  }, [live, listQ.data]);

  const liveIds = useMemo(() => new Set(live.slice(0, 15).map((e) => e.id)), [live]);

  // Follow tail: auto-snap to top when new events come in. User scroll disengages it.
  useEffect(() => {
    if (!followTail || !scrollRef.current) return;
    scrollRef.current.scrollTop = 0;
  }, [merged, followTail]);

  function applyQuickFilter(patch: Partial<EventFilters>) {
    setFilter((prev) => ({ ...prev, ...patch }));
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between gap-3 border-b border-border p-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">Event log</span>
          <span className="inline-flex items-center gap-1 text-[11px] text-status-success">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-status-success" /> live
          </span>
        </div>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" asChild>
            <a href={api.exportEventsUrl(effective, "csv")} target="_blank" rel="noreferrer">
              <Download className="h-3 w-3" /> CSV
            </a>
          </Button>
          <Button size="sm" variant="outline" asChild>
            <a href={api.exportEventsUrl(effective, "json")} target="_blank" rel="noreferrer">
              <Download className="h-3 w-3" /> JSON
            </a>
          </Button>
        </div>
      </header>

      <div className="border-b border-border p-2">
        <EventFiltersBar
          value={filter}
          onChange={(patch) => setFilter((prev) => ({ ...prev, ...patch }))}
          range={range}
          onRangeChange={setRange}
        />
      </div>

      <div className="border-b border-border p-2">
        <MetricsBar stats={statsQ.data} periodLabel={label} />
      </div>

      <div className="flex items-center gap-3 border-b border-border px-2 py-1 text-xs text-muted-foreground">
        <label className="inline-flex cursor-pointer items-center gap-1">
          <input
            type="checkbox"
            checked={followTail}
            onChange={(e) => setFollowTail(e.target.checked)}
          /> Follow tail
        </label>
        <span className="opacity-50">·</span>
        <span>Live: {live.length} · Archive: {listQ.data?.items.length ?? 0}</span>
      </div>

      <div
        ref={scrollRef}
        onWheel={() => setFollowTail(false)}
        className="flex-1 overflow-auto"
      >
        {listQ.isError && <div className="p-3"><ErrorBox title="Не удалось загрузить архив" detail={String(listQ.error)} /></div>}
        <ScrollArea className="h-full">
          <div className="flex flex-col">
            {merged.map((ev) => (
              <EventRow
                key={ev.id}
                ev={ev}
                isNew={liveIds.has(ev.id)}
                onOpen={setSelectedEvent}
                onQuickFilter={applyQuickFilter}
              />
            ))}
            {merged.length === 0 && !listQ.isLoading && (
              <div className="p-4 text-xs text-muted-foreground">событий в выбранном окне пока нет</div>
            )}
          </div>
        </ScrollArea>
      </div>

      <EventDetailDialog id={selectedEvent} onClose={() => setSelectedEvent(null)} onOpen={setSelectedEvent} />
    </div>
  );
}
