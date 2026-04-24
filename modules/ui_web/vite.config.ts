import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Dev server runs on :3000. /api/* and /media/* are proxied to FastAPI so
// the browser never needs to know about CORS or the backend port.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  const apiTarget = env.VITE_API_URL || "http://localhost:8000";

  return {
    plugins: [react()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    server: {
      host: "0.0.0.0",
      port: 3000,
      strictPort: true,
      // inotify не работает через Docker bind-mount с Windows-хоста —
      // без polling Vite не видит изменения в src/**.
      watch: { usePolling: true, interval: 300 },
      proxy: {
        "/api":    { target: apiTarget, changeOrigin: true, rewrite: p => p.replace(/^\/api/, "") },
        "/media":  { target: apiTarget, changeOrigin: true },
        "/events": { target: apiTarget, changeOrigin: true },
        "/accounts": { target: apiTarget, changeOrigin: true },
        "/dialogs":  { target: apiTarget, changeOrigin: true },
        "/messages": { target: apiTarget, changeOrigin: true },
        "/system":   { target: apiTarget, changeOrigin: true },
      },
    },
    preview: {
      host: "0.0.0.0",
      port: 3000,
      strictPort: true,
    },
  };
});
