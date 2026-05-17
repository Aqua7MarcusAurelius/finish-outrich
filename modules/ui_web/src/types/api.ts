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

export type LeadSource = "manual" | "partner_crm";
export type TargetGender = "any" | "female" | "male";

export interface Account {
  id: number;
  name: string;
  phone: string;
  status: WorkerStatus;
  is_active: boolean;
  dialogs_count: number;
  last_event_at: string | null;
  // 10.9: настройки источника лидов
  lead_source: LeadSource;
  target_gender: TargetGender;
}

// Информация о состоянии партнёрского CRM-модуля (для бэйджа).
export interface PartnerCrmHealth {
  configured: boolean;
  pool_size: number;
}

export interface PartnerCrmPoolItem {
  id: number;
  username: string;
  gender: "female" | "male" | null;
  fetched_at: string;
}

export interface LeadEvaluation {
  id: number;
  dialog_id: number;
  verdict: "success" | "archive";
  extracted_name: string | null;
  extracted_age: number | null;
  extracted_city: string | null;
  extracted_workplace: string | null;
  extracted_interests: string | null;
  evaluator_model: string | null;
  parse_succeeded: boolean;
  evaluated_at: string;
  updated_at: string;
}

// Per-worker AutoChat промт-конфиг (account_prompts).
// Два свободных текста; в каждом доступны плейсхолдеры — система
// подставляет реальные значения при генерации (см. generation.py).
// Пустой reply_template блокирует генерацию ответов; пустой
// initial_template блокирует POST /autochat/start.
export interface WorkerPrompts {
  account_id: number;
  initial_template: string;
  reply_template: string;
  updated_at: string | null;
}

// Операторский «пузырь». Каталог — в таблице dialog_statuses,
// управляется через страницу /settings. На диалоге хранится `status_id`,
// API отдаёт уже подцепленную карточку. null = «без статуса».
export interface DialogStatusRef {
  id: number;
  label: string;
  color: string;
}

export interface DialogSummary {
  id: number;
  account_id: number;
  title: string;
  username: string | null;
  phone: string | null;
  is_bot: boolean;
  is_contact: boolean;
  status: DialogStatusRef | null;
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

// Per-account автомат старта диалогов. Очередь — список юзернеймов
// (без `@`, бэк нормализует). interval_sec — минимум 60 (защита от флуда).
export interface AutoRunnerState {
  account_id: number;
  is_running: boolean;
  queue: string[];
  interval_sec: number;
  jitter_sec: number;
  last_action_at: string | null;
  last_status: string | null;
  updated_at: string | null;
}

export interface AutoRunnerPatchBody {
  queue?: string[];
  interval_sec?: number;
  jitter_sec?: number;
}

export interface EventFilters {
  account?: number;
  module?: BusModule;
  type?: string;
  status?: EventStatus;
  dialog_id?: number;
  from?: string;
  to?: string;
  limit?: number;
  cursor?: string;
}
