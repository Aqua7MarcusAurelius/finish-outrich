import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, openEventStream } from "@/lib/api";
import { ModulePill } from "@/components/common/ModulePill";
import { StatusDot } from "@/components/common/StatusDot";
import { ErrorBox } from "@/components/common/ErrorBox";
import { cn } from "@/lib/utils";
import type { BusEvent } from "@/types/api";

// Мини event log сбоку от чата: последние события шины по текущему диалогу.
// Архив + live SSE, дедуп по id, плотная колонка. Серверный фильтр
// dialog_id добавлен в 8.13 — UI просто передаёт dialog_id в оба канала.

const LIVE_BUFFER = 100;
const ARCHIVE_LIMIT = 50;

function fmtTime(iso: string) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function shortId(id: string) { return id.slice(0, 8); }

export function DialogEventsWidget({ dialogId }: { dialogId: number | null }) {
  const archiveQ = useQuery({
    queryKey: ["dialog-events", dialogId],
    queryFn: () => api.listEvents({ dialog_id: dialogId!, limit: ARCHIVE_LIMIT }),
    enabled: dialogId != null,
    refetchInterval: 10_000,
  });

  const [live, setLive] = useState<BusEvent[]>([]);
  const freshIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (dialogId == null) { setLive([]); freshIdsRef.current = new Set(); return; }
    setLive([]);
    freshIdsRef.current = new Set();
    const src = openEventStream({ dialog_id: dialogId }, (e) => {
      freshIdsRef.current.add(e.id);
      setLive((prev) => {
        const next = [e, ...prev];
        return next.length > LIVE_BUFFER ? next.slice(0, LIVE_BUFFER) : next;
      });
    });
    return () => { src.close(); };
  }, [dialogId]);

  const merged = useMemo(() => {
    const map = new Map<string, BusEvent>();
    for (const e of live) map.set(e.id, e);
    for (const e of archiveQ.data?.items ?? []) if (!map.has(e.id)) map.set(e.id, e);
    return [...map.values()].sort((a, b) => b.time.localeCompare(a.time));
  }, [live, archiveQ.data]);

  if (dialogId == null) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-center text-xs text-muted-foreground">
        Выбери диалог чтобы увидеть его поток событий.
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex h-12 shrink-0 items-center border-b border-border px-3">
        <span className="text-sm font-semibold">Events по диалогу</span>
        <span className="mono ml-2 text-[11px] text-muted-foreground">dialog #{dialogId}</span>
        <span className="ml-auto inline-flex items-center gap-1 text-[11px] text-status-success">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-status-success" /> live
        </span>
      </header>

      <div className="flex-1 overflow-y-auto">
        {archiveQ.isError && (
          <div className="p-2">
            <ErrorBox title="Архив событий не загрузился" detail={String(archiveQ.error)} />
          </div>
        )}
        {archiveQ.isLoading && merged.length === 0 && (
          <div className="p-3 text-xs text-muted-foreground">загрузка…</div>
        )}
        {!archiveQ.isLoading && merged.length === 0 && (
          <div className="p-3 text-xs text-muted-foreground">событий по этому диалогу пока нет</div>
        )}
        {merged.map((ev) => {
          const isFresh = freshIdsRef.current.has(ev.id);
          return (
            <div
              key={ev.id}
              className={cn(
                "flex items-start gap-2 border-b border-border/40 px-3 py-1.5 text-[11px]",
                ev.status === "error" && "bg-status-error/10",
                isFresh && "animate-flash-new",
              )}
            >
              <StatusDot status={ev.status} className="mt-1 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="mono text-muted-foreground">{fmtTime(ev.time)}</span>
                  <ModulePill module={ev.module} />
                </div>
                <div className="mono mt-0.5 truncate font-semibold" title={ev.id}>
                  {ev.type}
                </div>
                {ev.message && (
                  <div className="mt-0.5 truncate text-muted-foreground" title={ev.message}>
                    {ev.message}
                  </div>
                )}
                <div className="mono mt-0.5 text-[10px] text-muted-foreground">#{shortId(ev.id)}</div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
