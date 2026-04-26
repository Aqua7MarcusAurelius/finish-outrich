import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { DialogsPage } from "@/pages/DialogsPage";
import { EventLogPage } from "@/pages/EventLogPage";
import { WorkerPromptPage } from "@/pages/WorkerPromptPage";

export default function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Navigate to="/dialogs" replace />} />
        <Route path="/dialogs" element={<DialogsPage />} />
        <Route path="/dialogs/:accountId" element={<DialogsPage />} />
        <Route path="/dialogs/:accountId/:dialogId" element={<DialogsPage />} />
        <Route path="/events" element={<EventLogPage />} />
        <Route path="/workers/:accountId/prompt" element={<WorkerPromptPage />} />
        <Route path="*" element={<Navigate to="/dialogs" replace />} />
      </Routes>
    </AppShell>
  );
}
