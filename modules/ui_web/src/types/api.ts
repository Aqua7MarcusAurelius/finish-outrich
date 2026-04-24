// Types mirror docs/ui/web_ui_api_contract_v1.md. Kept loose where the
// contract explicitly says "all fields of table X" so the UI survives
// additive schema changes on the backend.

// Mirrors core/events.py::Module. Kept as a string-union of *known* modules
// but components should tolerate unknown values (ModulePill has a fallback).
export type BusModule =
  | "history"
  | "history_sync"
  | "transcription"
  | "description"
  | "wrapper"
  | "worker"
  | "worker_manager"
  | "auth"
  | "cleaner"
  | "api"
  | "system"
  | "bus"
  | "autochat";

export type EventStatus = "success" | "error" | "in_progress";
export type WorkerStatus =
  | "running"
  | "starting"
  | "stopping"
  | "stopped"
  | "crashed"
  | "session_expired";

export type MediaType = "photo" | "video" | "voice" | "video_note" | "document" | "audio" | "sticker" | "animation";
export type AsyncJobStatus = "none" | "pending" | "done" | "failed";

export interface Account {
  id: number;
  name: string;
  phone: string;
  status: WorkerStatus;
  is_active: boolean;
  dialogs_count: number;
  last_event_at: string | null;
}

export interface DialogSummary {
  id: number;
  account_id: number;
  title: string;
  username: string | null;
  phone: string | null;
  is_bot: boolean;
  is_contact: boolean;
  last_message: { text: string | null; date: string; type: string } | null;
}

export interface DialogProfile {
  id: number;
  account_id: number;
  first_name: string | null;
  last_name: string | null;
  username: string | null;
  phone: string | null;
  is_bot: boolean;
  is_contact: boolean;
  contact_first_name: string | null;
  contact_last_name: string | null;
  [k: string]: unknown;
}

export interface Reaction {
  emoji: string;
  count: number;
  is_outgoing: boolean;
  created_at: string;
  removed_at: string | null;
}

export interface MediaItem {
  id: number;
  type: MediaType;
  mime_type: string | null;
  duration: number | null;
  width: number | null;
  height: number | null;
  size_bytes: number | null;
  file_name: string | null;
  storage_key: string | null;
  preview_url: string;
  transcription: string | null;
  transcription_status: AsyncJobStatus;
  description: string | null;
  description_status: AsyncJobStatus;
}

export interface ForwardInfo {
  from_username: string | null;
  from_name: string | null;
  from_chat_title: string | null;
  date: string;
}

export interface Message {
  id: number;
  telegram_message_id: number;
  is_outgoing: boolean;
  date: string;
  text: string | null;
  type: string;
  edited_at: string | null;
  deleted_at: string | null;
  reply_to: { message_id: number; text_preview: string | null; is_outgoing: boolean } | null;
  forward: ForwardInfo | null;
  media_group_id: string | null;
  media: MediaItem[];
  reactions: Reaction[];
}

export interface MessageEdit {
  id: number;
  message_id: number;
  previous_text: string | null;
  new_text: string | null;
  edited_at: string;
}

export interface Paginated<T> {
  items: T[];
  next_cursor?: string;
  has_more: boolean;
}

// ── Events ──────────────────────────────────────────────────────────────

// Event id / parent_id are 32-char hex strings (see core/bus.py), not ints.
export interface BusEvent {
  id: string;
  parent_id: string | null;
  time: string;
  account_id: number | null;
  account_name: string | null;
  module: BusModule;
  type: string;
  status: EventStatus;
  data: Record<string, unknown>;
  message?: string;
}

export interface EventChain {
  ancestors: Pick<BusEvent, "id" | "module" | "type" | "status">[];
  event: BusEvent;
  descendants: Pick<BusEvent, "id" | "module" | "type" | "status">[];
}

export interface EventStats {
  total: number;
  success: number;
  error: number;
  in_progress: number;
  events_per_sec: number;
}

export interface EventFilters {
  account?: number;
  module?: BusModule;
  type?: string;
  status?: EventStatus;
  from?: string;
  to?: string;
  limit?: number;
  cursor?: string;
}
