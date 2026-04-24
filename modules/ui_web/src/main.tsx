import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { ToastProvider } from "@/lib/toast";
import "./index.css";

// HashRouter, не BrowserRouter: под ним SPA-пути живут после `#`, сервер
// их не видит и не может случайно спутать /events (страница) с /events
// (API-ручка FastAPI, которую Vite прокси-ит на бэкенд). Иначе F5 на
// /events возвращал сырой JSON из бэкенда.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      staleTime: 10_000,
      retry: 1,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <HashRouter>
          <App />
        </HashRouter>
      </ToastProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
