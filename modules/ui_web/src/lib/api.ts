import type {
  Account,
  BusEvent,
  DialogProfile,
  DialogSummary,
  EventChain,
  EventFilters,
  EventStats,
  Message,
  MessageEdit,
  Paginated,
} from "@/types/api";

// All requests are same-origin — the Vite dev server proxies to FastAPI.
// In prod a reverse proxy does the same, so paths match the API contract 1:1.

export class ApiError extends Error {
  constructor(public status: number, public body: unknown, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown = body;
    try { parsed = JSON.parse(body); } catch { /* keep as text */ }
    throw new ApiError(res.status, parsed, `${init?.method ?? "GET"} ${path} -> ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

function qs(params: Record<string, unknown>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  if (entries.length === 0) return "";
  const s = new URLSearchParams();
  for (const [k, v] of entries) s.set(k, String(v));
  return `?${s.toString()}`;
}

export const api = {
  // ── accounts & dialogs ──────────────────────────────────────────────
  listAccounts: () =>
    request<Account[]>("/accounts"),

  listDialogs: (accountId: number, cursor?: string) =>
    request<Paginated<DialogSummary>>(`/accounts/${accountId}/dialogs${qs({ cursor })}`),

  getDialog: (dialogId: number) =>
    request<DialogProfile>(`/dialogs/${dialogId}`),

  listMessages: (dialogId: number, cursor?: string, limit = 50) =>
    request<Paginated<Message>>(`/dialogs/${dialogId}/messages${qs({ cursor, limit })}`),

  listMessageEdits: (messageId: number) =>
    request<MessageEdit[]>(`/messages/${messageId}/edits`),

  mediaPreviewUrl: (mediaId: number) => `/media/${mediaId}/preview`,

  // ── worker control ──────────────────────────────────────────────────
  startWorker: (accountId: number) =>
    request<void>(`/workers/${accountId}/start`, { method: "POST" }),
  stopWorker: (accountId: number) =>
    request<void>(`/workers/${accountId}/stop`, { method: "POST" }),

  // ── events ──────────────────────────────────────────────────────────
  listEvents: (f: EventFilters) =>
    request<Paginated<BusEvent>>(`/events${qs({ ...f })}`),

  eventStats: (f: EventFilters) =>
    request<EventStats>(`/events/stats${qs({ ...f })}`),

  getEvent: (id: number) =>
    request<BusEvent>(`/events/${id}`),

  eventChain: (id: number) =>
    request<EventChain>(`/events/${id}/chain`),

  exportEventsUrl: (f: EventFilters, format: "csv" | "json") =>
    `/events/export${qs({ ...f, format })}`,
};

// SSE helper: opens /events/stream with filters, reopens on filter change.
export function openEventStream(
  filters: Pick<EventFilters, "account" | "module" | "type" | "status">,
  onEvent: (e: BusEvent) => void,
  onError?: (ev: Event) => void,
): EventSource {
  const url = `/events/stream${qs({ ...filters })}`;
  const source = new EventSource(url);
  source.addEventListener("event", (ev) => {
    try {
      onEvent(JSON.parse((ev as MessageEvent).data) as BusEvent);
    } catch {
      // ignore malformed lines — SSE stream is best-effort live data
    }
  });
  if (onError) source.addEventListener("error", onError);
  return source;
}
