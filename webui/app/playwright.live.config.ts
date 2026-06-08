import { defineConfig } from "@playwright/test";

// LIVE-BACKEND E2E: drives the real UI against the real FastAPI server (webui.server.demo) over a
// seeded in-memory registry — NO API mocking. This is the run that proves the genuine
// UI <-> API <-> pure-core contract (the mocked smoke in playwright.config.ts cannot). The server
// static-mounts the built UI, so UI + API are same-origin on :8788.
export default defineConfig({
  testDir: "./e2e-live",
  timeout: 60_000,
  use: { baseURL: "http://127.0.0.1:8788" },
  webServer: {
    command: "python -m uvicorn webui.server.demo:app --host 127.0.0.1 --port 8788",
    url: "http://127.0.0.1:8788/api/health",
    timeout: 120_000,
    reuseExistingServer: true,
    cwd: ".",
    env: {
      PYTHONPATH: "../..",
      OL_SUPERVISOR_WEBUI_STATIC: "dist",
      PYTHONIOENCODING: "utf-8",
    },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
