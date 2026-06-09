import { defineConfig } from "@playwright/test";

// REAL-DB E2E (opt-in): drives the UI against the REAL app (webui.server.app) wired to the live
// Registry → the actual database. Proves the UI ↔ API ↔ Registry ↔ DB path through the browser
// (the live.config.ts run uses a seeded in-memory registry). Requires OL_SUPERVISOR_DB_URL in the
// environment (point it at the disposable dev branch — NEVER prod). Read-only: it asserts the
// console renders real data with zero runtime errors; it does not mutate the DB.
//
// Run:  $env:OL_SUPERVISOR_DB_URL = "<dev-branch DSN>"; npx playwright test -c playwright.db.config.ts
export default defineConfig({
  testDir: "./e2e-db",
  timeout: 60_000,
  use: { baseURL: "http://127.0.0.1:8789" },
  webServer: {
    command: "python -m uvicorn webui.server.app:app --host 127.0.0.1 --port 8789",
    url: "http://127.0.0.1:8789/api/health",
    timeout: 120_000,
    reuseExistingServer: true,
    cwd: ".",
    env: {
      PYTHONPATH: "../..",
      OL_SUPERVISOR_WEBUI_STATIC: "dist",
      OL_SUPERVISOR_DB_URL: process.env.OL_SUPERVISOR_DB_URL ?? "",
      PYTHONIOENCODING: "utf-8",
    },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
