/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The control panel talks to the FastAPI server (webui.server.app) on :8787.
// In dev, proxy /api there so the browser sees a same-origin API.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8787", changeOrigin: true },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    // Playwright specs live under e2e/ + e2e-live/ and are run by Playwright, not Vitest.
    exclude: ["e2e/**", "e2e-live/**", "e2e-db/**", "node_modules/**"],
  },
});
