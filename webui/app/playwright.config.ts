import { defineConfig } from "@playwright/test";

// E2E runs against the BUILT app served by `vite preview` on :4173. The API is mocked via
// page.route in the spec, so the E2E needs neither the Python server nor a DB — it verifies the
// real browser render + interaction + fetch wiring. (A live-backend E2E is a later phase.)
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:4173" },
  webServer: {
    command: "npm run build && npm run preview",
    url: "http://127.0.0.1:4173",
    timeout: 120_000,
    reuseExistingServer: true,
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
