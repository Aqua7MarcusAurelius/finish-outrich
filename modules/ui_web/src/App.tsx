import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { DialogsPage } from "@/pages/DialogsPage";
import { EventLogPage } from "@/pages/EventLogPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { WorkerPromptPage } from "@/pages/WorkerPromptPage";
import { useLiveInvalidator } from "@/hooks/useLiveInvalidator";

export default function App() {
  // Один SSE-подписчик на всё приложение — при каждом релевантном
  // событии из шины инвалидирует соответствующий React-Query кэш.
  // Заменяет polling refetchInterval в отдельных хуках.
  useLiveInvalidator();
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Navigate to="/dialogs" replace />} />
        <Route path="/dialogs" element={<DialogsPage />} />
        <Route path="/dialogs/:accountId" element={<DialogsPage />} />
        <Route path="/dialogs/:accountId/:dialogId" element={<DialogsPage />} />
        <Route path="/events" element={<EventLogPage />} />
        <Route path="/workers/:accountId/prompt" element={<WorkerPromptPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/dialogs" replace />} />
      </Routes>
    </AppShell>
  );
}
