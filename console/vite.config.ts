import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * Vite configuration for the tgobjects3 admin console.
 *
 * In development the console runs on its own dev server and proxies all
 * `/admin/*` and `/health/*` calls to the gateway Admin API (default
 * `ADMIN_PORT` = 8080). The production build emits static assets into
 * `dist/`, which `deploy/` serves behind nginx (referenced by
 * `deploy/docker-compose.yml` as `deploy/Dockerfile.console`).
 */
const ADMIN_API_TARGET = process.env.ADMIN_API_URL ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/admin": {
        target: ADMIN_API_TARGET,
        changeOrigin: true,
      },
      "/health": {
        target: ADMIN_API_TARGET,
        changeOrigin: true,
      },
      "/metrics": {
        target: ADMIN_API_TARGET,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
