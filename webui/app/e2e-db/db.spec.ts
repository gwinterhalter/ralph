import { test, expect } from "@playwright/test";

// Drives the UI against the REAL FastAPI app wired to the live Registry → real DB. Read-only:
// proves the full stack renders real data through the browser with zero runtime errors. Structural
// assertions (not data-specific) so it holds whether the dev branch is busy or empty.

test("real-DB console renders across tabs with zero runtime errors", async ({ page }) => {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const badResponses: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  page.on("response", (r) => { if (r.status() >= 400) badResponses.push(`${r.status()} ${r.url()}`); });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Needs You" })).toBeVisible();
  await page.screenshot({ path: "test-results/db-00-home.png", fullPage: true });

  // Fleet — ALL real projects (incl your completed/failed/paused_gate), not just active.
  await page.getByRole("button", { name: "Fleet", exact: true }).click();
  await expect(page.getByRole("columnheader", { name: "Project" })).toBeVisible();
  // your dev branch has 5 projects → the table has real rows (the "fleet is empty" fix).
  await expect(page.locator("table.fleet tbody tr").first()).toBeVisible();
  await page.screenshot({ path: "test-results/db-02-fleet.png", fullPage: true });

  // Runs — your real past runs ($10.74 across 5).
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Run history" })).toBeVisible();
  await page.screenshot({ path: "test-results/db-03-runs.png", fullPage: true });

  // Events — /api/events hit the real DB; the rollup ("<n> event(s)") always renders.
  await page.getByRole("button", { name: "Events", exact: true }).click();
  await expect(page.getByText(/event\(s\)/)).toBeVisible();

  // Spend — /api/forecast over the real learning_records.
  await page.getByRole("button", { name: "Spend", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Spend" })).toBeVisible();
  await expect(page.getByText(/Fleet spent \$/)).toBeVisible();

  // Graph — /api/graph over the real projects table.
  await page.getByRole("button", { name: "Graph", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Dependency graph" })).toBeVisible();

  await page.screenshot({ path: "test-results/db-01-real.png", fullPage: true });
  expect(consoleErrors, "browser console errors").toEqual([]);
  expect(pageErrors, "uncaught page errors").toEqual([]);
  expect(badResponses, "failed/4xx-5xx responses").toEqual([]);
});
