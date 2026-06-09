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

  // Fleet — the real snapshot table renders (header present whether or not rows exist).
  await page.getByRole("button", { name: "Fleet", exact: true }).click();
  await expect(page.getByRole("columnheader", { name: "Project" })).toBeVisible();

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
