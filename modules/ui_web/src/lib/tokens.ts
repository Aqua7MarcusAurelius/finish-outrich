import type { BusModule, EventStatus, WorkerStatus } from "@/types/api";

// Single source of truth mapping bus-module name -> Tailwind token name.
// Extend here when new modules appear on the bus.
export const MODULE_TOKEN: Record<BusModule, string> = {
  telegram:       "module-telegram",
  history:        "module-history",
  transcription:  "module-transcription",
  description:    "module-description",
  auth:           "module-auth",
  worker_manager: "module-worker_manager",
  autochat:       "module-autochat",
};

export const MODULE_LABEL: Record<BusModule, string> = {
  telegram:       "telegram",
  history:        "history",
  transcription:  "transcription",
  description:    "description",
  auth:           "auth",
  worker_manager: "worker_manager",
  autochat:       "autochat",
};

export const STATUS_TOKEN: Record<EventStatus, string> = {
  success:     "status-success",
  error:       "status-error",
  in_progress: "status-in_progress",
};

export const WORKER_STATUS_TOKEN: Record<WorkerStatus, string> = {
  running:          "status-success",
  crashed:          "status-error",
  starting:         "status-in_progress",
  stopping:         "status-in_progress",
  stopped:          "status-stopped",
  session_expired:  "status-error",
};
