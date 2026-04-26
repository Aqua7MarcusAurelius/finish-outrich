import type {
  Account,
  BusEvent,
  DialogProfile,
  DialogSummary,
  EventChain,
  EventFilters,
  EventStats,
  MediaItem,
  Message,
  MessageEdit,
  Paginated,
  WorkerPrompts,
} from "@/types/api";

// All requests are same-origin — the Vite dev server proxies to FastAPI.
// In prod a reverse proxy does the same, so paths match the API contract 1:1.

export class ApiError extends Error {
  constructor(public status: number, public body: unknown, message: string) {
    super(message);
  }
}

// Вытащить человекочитаемое сообщение из { error: { code, message } } shape,
// который возвращают FastAPI-ручки проекта. Фолбэк — general .message.
export function describeApiError(e: unknown): { title: string; detail?: string } {
  if (e instanceof ApiError) {
    const b = e.body as { error?: { code?: string; message?: string } } | string | null;
    if (b && typeof b === "object" && b.error) {
      return {
        title: b.error.message || b.error.code || `HTTP ${e.status}`,
        detail: b.error.code ? `${b.error.code} (HTTP ${e.status})` : `HTTP ${e.status}`,
      };
    }
    return { title: `HTTP ${e.status}`, detail: typeof b === "string" ? b : undefined };
  }
  return { title: String(e) };
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

// ──────────────────────────────────────────────────────────────────────
// Adapters — normalize backend response shapes into the contract shapes
// the UI components consume. The backend predates the contract for some
// endpoints (history/routes.py uses forward_from/reply_to_message_id etc.),
// so we translate here instead of renaming fields in a hot shared module.
// ──────────────────────────────────────────────────────────────────────

type BackendDialog = {
  id: number;
  account_id: number;
  first_name: string | null;
  last_name: string | null;
  username: string | null;
  phone: string | null;
  is_bot: boolean;
  is_contact: boolean;
  last_message: { date: string; text: string | null; is_outgoing: boolean } | null;
};

function adaptDialog(d: BackendDialog): DialogSummary {
  const name = [d.first_name, d.last_name].filter(Boolean).join(" ").trim();
  const title = name || d.username || "(без имени)";
  return {
    id: d.id,
    account_id: d.account_id,
    title,
    username: d.username,
    phone: d.phone,
    is_bot: d.is_bot,
    is_contact: d.is_contact,
    last_message: d.last_message
      ? {
          text: d.last_message.text,
          date: d.last_message.date,
          type: d.last_message.text ? "text" : "media",
        }
      : null,
  };
}

type BackendMedia = {
  id: number;
  type: MediaItem["type"];
  mime_type: string | null;
  duration: number | null;
  width: number | null;
  height: number | null;
  file_size: number | null;
  file_name: string | null;
  storage_key: string | null;
  transcription: string | null;
  transcription_status: MediaItem["transcription_status"];
  description: string | null;
  description_status: MediaItem["description_status"];
};

function adaptMedia(m: BackendMedia): MediaItem {
  return {
    id: m.id,
    type: m.type,
    mime_type: m.mime_type,
    duration: m.duration,
    width: m.width,
    height: m.height,
    size_bytes: m.file_size,
    file_name: m.file_name,
    storage_key: m.storage_key,
    preview_url: `/media/${m.id}/preview`,
    transcription: m.transcription,
    transcription_status: m.transcription_status ?? "none",
    description: m.description,
    description_status: m.description_status ?? "none",
  };
}

type BackendMessage = {
  id: number;
  dialog_id: number;
  telegram_message_id: number;
  is_outgoing: boolean;
  type: string;
  date: string;
  text: string | null;
  reply_to_message_id: number | null;
  reply_to_telegram_message_id: number | null;
  forward_from: { user_id: number | null; username: string | null; name: string | null; chat_id: number | null; date: string } | null;
  media_group_id: string | null;
  edited_at: string | null;
  deleted_at: string | null;
  media: BackendMedia[];
  reactions: { emoji: string | null; custom_emoji_id: string | null; is_outgoing: boolean; created_at: string; removed_at: string | null }[];
};

function adaptMessage(m: BackendMessage): Message {
  // Group reactions by emoji/custom_emoji_id so the UI shows one pill per kind
  // with a count — matches how Telegram displays them and the `Reaction`
  // contract shape.
  const counts = new Map<string, { emoji: string; count: number; is_outgoing: boolean; created_at: string }>();
  for (const r of m.reactions) {
    const key = r.emoji ?? `custom:${r.custom_emoji_id ?? ""}`;
    const prev = counts.get(key);
    if (prev) {
      prev.count += 1;
      if (r.is_outgoing) prev.is_outgoing = true;
    } else {
      counts.set(key, {
        emoji: r.emoji ?? "❓",
        count: 1,
        is_outgoing: r.is_outgoing,
        created_at: r.created_at,
      });
    }
  }

  return {
    id: m.id,
    telegram_message_id: m.telegram_message_id,
    is_outgoing: m.is_outgoing,
    date: m.date,
    text: m.text,
    type: m.type,
    edited_at: m.edited_at,
    deleted_at: m.deleted_at,
    reply_to: m.reply_to_message_id
      ? {
          message_id: m.reply_to_message_id,
          text_preview: null,
          is_outgoing: false,
        }
      : null,
    forward: m.forward_from
      ? {
          from_username: m.forward_from.username,
          from_name: m.forward_from.name,
          from_chat_title: null,
          date: m.forward_from.date,
        }
      : null,
    media_group_id: m.media_group_id,
    media: (m.media ?? []).map(adaptMedia),
    reactions: [...counts.values()].map((r) => ({
      emoji: r.emoji,
      count: r.count,
      is_outgoing: r.is_outgoing,
      created_at: r.created_at,
      removed_at: null,
    })),
  };
}

// ──────────────────────────────────────────────────────────────────────

export const api = {
  // ── accounts & dialogs ──────────────────────────────────────────────
  listAccounts: () =>
    request<Account[]>("/accounts"),

  listDialogs: async (accountId: number): Promise<Paginated<DialogSummary>> => {
    const raw = await request<{ dialogs: BackendDialog[] }>(`/accounts/${accountId}/dialogs`);
    const items = (raw.dialogs ?? []).map(adaptDialog);
    return { items, has_more: false };
  },

  getDialog: (dialogId: number) =>
    request<DialogProfile>(`/dialogs/${dialogId}`),

  // Полное жёсткое удаление диалога: останавливается активная autochat-
  // сессия (если есть), удаляются MinIO-файлы media, каскадом сносятся
  // messages/media/reactions/edits. После — собеседник для системы новый.
  deleteDialog: (dialogId: number) =>
    request<void>(`/dialogs/${dialogId}`, { method: "DELETE" }),

  // Toggle автодиалога для существующего диалога. POST включает (без
  // отправки initial — просто active-сессия ждёт входящих), DELETE гасит
  // активную сессию. GET возвращает текущее состояние.
  getDialogAutochat: (dialogId: number) =>
    request<{ active: boolean; session_id: number | null; status: string | null }>(
      `/dialogs/${dialogId}/autochat`,
    ),
  enableDialogAutochat: (dialogId: number) =>
    request<{ session: { id: number; status: string; [k: string]: unknown } }>(
      `/dialogs/${dialogId}/autochat`,
      { method: "POST" },
    ),
  disableDialogAutochat: (dialogId: number) =>
    request<{ session: { id: number; status: string; [k: string]: unknown } }>(
      `/dialogs/${dialogId}/autochat`,
      { method: "DELETE" },
    ),

  listMessages: async (dialogId: number, cursor?: string, limit = 50): Promise<Paginated<Message>> => {
    const raw = await request<{ messages: BackendMessage[]; next_cursor: string | null }>(
      `/dialogs/${dialogId}/messages${qs({ cursor, limit })}`,
    );
    const items = (raw.messages ?? []).map(adaptMessage);
    return {
      items,
      next_cursor: raw.next_cursor ?? undefined,
      has_more: !!raw.next_cursor,
    };
  },

  listMessageEdits: async (messageId: number): Promise<MessageEdit[]> => {
    const raw = await request<{ id: number; message_id: number; old_text: string | null; edited_at: string }[]>(
      `/messages/${messageId}/edits`,
    );
    return raw.map((r) => ({
      id: r.id,
      message_id: r.message_id,
      previous_text: r.old_text,
      new_text: null,
      edited_at: r.edited_at,
    }));
  },

  mediaPreviewUrl: (mediaId: number) => `/media/${mediaId}/preview`,

  // ── worker control ──────────────────────────────────────────────────
  startWorker: (accountId: number) =>
    request<void>(`/workers/${accountId}/start`, { method: "POST" }),
  stopWorker: (accountId: number) =>
    request<void>(`/workers/${accountId}/stop`, { method: "POST" }),

  // ── per-worker prompts (AutoChat) ───────────────────────────────────
  // 8 структурированных полей для reply + одно initial_system. Если строки
  // в БД ещё нет, GET отдаёт дефолты для forbidden/format_reply (overlay,
  // не персистится автоматически). Все 8 reply-полей пустые на момент
  // генерации = блок автоответа (см. session.py::_generate_and_enqueue).
  getWorkerPrompts: (accountId: number) =>
    request<WorkerPrompts>(`/accounts/${accountId}/prompts`),
  saveWorkerPrompts: (accountId: number, body: Omit<WorkerPrompts, "account_id" | "updated_at">) =>
    request<WorkerPrompts>(`/accounts/${accountId}/prompts`, {
      method: "PUT", body: JSON.stringify(body),
    }),

  // Превью того, что уйдёт в chat_completion. LLM НЕ вызывается, токены не
  // тратятся. Принимает текущие значения полей (можно несохранённые) +
  // опц. dialog_id (источник истории) + опц. user_system_prompt (заметка
  // про собеседника). Без dialog_id — placeholder вместо истории.
  previewWorkerPrompts: (accountId: number, body: {
    fabula: string; bio: string; style: string; forbidden: string;
    length_hint: string; goals: string; format_reply: string; examples: string;
    dialog_id?: number | null;
    user_system_prompt?: string;
  }) =>
    request<{ system: string; user: string; dialog_id: number | null }>(
      `/accounts/${accountId}/prompts/preview`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  // ── auth (добавление нового аккаунта) ───────────────────────────────
  // Возвращаемые поля — modules/auth/service.py: PHASE_CODE_SENT /
  // PHASE_2FA_REQUIRED / PHASE_DONE. `done` несёт account_id.
  authStart: (body: { phone: string; name: string; proxy_primary: string; proxy_fallback: string }) =>
    request<{ session_id: string; status: "code_sent" }>("/auth/start", {
      method: "POST", body: JSON.stringify(body),
    }),
  authCode: (session_id: string, code: string) =>
    request<{ status: "2fa_required" | "done"; account_id?: number }>("/auth/code", {
      method: "POST", body: JSON.stringify({ session_id, code }),
    }),
  auth2fa: (session_id: string, password: string) =>
    request<{ status: "done"; account_id?: number }>("/auth/2fa", {
      method: "POST", body: JSON.stringify({ session_id, password }),
    }),
  authCancel: (session_id: string) =>
    request<void>(`/auth/${session_id}`, { method: "DELETE" }),

  // ── autochat (создание авто-диалога) ────────────────────────────────
  // POST /autochat/start запускает сессию: резолвит @username, генерирует
  // первое сообщение по per-worker initial_system из account_prompts
  // (через Opus 4.7 в OpenRouter), отправляет его и дальше ведёт
  // переписку. Per-session inputs убраны — body = {account_id, username}.
  autochatStart: (body: { account_id: number; username: string }) =>
    request<{ session: { id: number; status: string; dialog_id: number | null; [k: string]: unknown } }>(
      "/autochat/start",
      { method: "POST", body: JSON.stringify(body) },
    ),

  // ── events ──────────────────────────────────────────────────────────
  listEvents: async (f: EventFilters): Promise<Paginated<BusEvent>> => {
    const raw = await request<{ events: BusEvent[]; next_cursor: string | null }>(
      `/events${qs({ ...f })}`,
    );
    return {
      items: raw.events ?? [],
      next_cursor: raw.next_cursor ?? undefined,
      has_more: !!raw.next_cursor,
    };
  },

  eventStats: (f: EventFilters) =>
    request<EventStats>(`/events/stats${qs({ ...f })}`),

  getEvent: (id: string) =>
    request<BusEvent>(`/events/${id}`),

  eventChain: (id: string) =>
    request<EventChain>(`/events/${id}/chain`),

  exportEventsUrl: (f: EventFilters, format: "csv" | "json") =>
    `/events/export${qs({ ...f, format })}`,
};

// SSE helper: opens /events/stream with filters, reopens on filter change.
export function openEventStream(
  filters: Pick<EventFilters, "account" | "module" | "type" | "status" | "dialog_id">,
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
