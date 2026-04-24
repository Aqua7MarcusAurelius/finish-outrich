import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, openEventStream } from "@/lib/api";
import type { BusEvent, EventFilters } from "@/types/api";

export function useEventList(filters: EventFilters) {
  return useQuery({
    queryKey: ["events", filters],
    queryFn: () => api.listEvents(filters),
    refetchInterval: 15_000,
  });
}

export function useEventStats(filters: EventFilters) {
  return useQuery({
    queryKey: ["event-stats", filters],
    queryFn: () => api.eventStats(filters),
    refetchInterval: 5_000,
  });
}

// Live SSE stream. Reopens the EventSource whenever filters change.
export function useEventStream(filters: Pick<EventFilters, "account" | "module" | "type" | "status">) {
  const [live, setLive] = useState<BusEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    setLive([]);
    const src = openEventStream(filters, (e) => {
      setLive((prev) => {
        const next = [e, ...prev];
        return next.length > 500 ? next.slice(0, 500) : next;
      });
    }, () => setConnected(false));
    src.onopen = () => setConnected(true);
    sourceRef.current = src;
    return () => { src.close(); sourceRef.current = null; setConnected(false); };
  }, [filters.account, filters.module, filters.type, filters.status]);

  return { live, connected };
}
