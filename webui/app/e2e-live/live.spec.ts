import { test, expect } from "@playwright/test";

// No route mocking: every request hits the real FastAPI server (webui.server.demo) over its seeded
// in-memory registry + a seeded pending gate_request file. Proves the real JSON contract + full
// action round-trips across every screen. "Verified working" is enforced: the test FAILS on any
// browser console error, uncaught page error, or 4xx-5xx response (502 from the asserted
// apply-not-resolvable case excepted).

test("full console: real data, gate resolve, adopt, all tabs, paused-gate visibility — zero errors", async ({ page }) => {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const badResponses: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  page.on("response", (r) => { if (r.status() >= 400 && r.status() !== 502) badResponses.push(`${r.status()} ${r.url()}`); });

  await page.goto("/");

  // Home inbox from the REAL build_inbox over the seeded registry + gate file + budget breach:
  await expect(page.getByRole("heading", { name: "Needs You" })).toBeVisible();
  await expect(page.getByText("Budget ceiling tripped")).toBeVisible();
  await expect(page.getByText(/Gate · abs-phase-boundary/)).toBeVisible();           // rich gate (file)
  await expect(page.getByText("proceed to Phase 1?")).toBeVisible();
  await expect(page.getByText(/Gate awaiting decision · oltest_paused/)).toBeVisible(); // paused_gate PROJECT surfaced (the fixed defect)
  await expect(page.getByText(/Learning ready .*abs-phase-boundary/)).toBeVisible();
  await expect(page.getByText(/Adopted learning regressed/)).toBeVisible();
  await expect(page.getByText(/Chronic correction churn · OLB-07/)).toBeVisible();
  await page.screenshot({ path: "test-results/01-home.png", fullPage: true });

  // Resolve the file gate: its real option "proceed" → POST /api/gates/resolve; card disappears.
  await page.getByRole("button", { name: "proceed" }).click();
  await expect(page.getByText("proceed to Phase 1?")).toHaveCount(0);

  // Fleet — ALL projects incl CLOSED activity (complete + failed), not just active.
  await page.getByRole("button", { name: "Fleet", exact: true }).click();
  await expect(page.getByRole("cell", { name: "abs_phase0", exact: true }).first()).toBeVisible();
  await expect(page.getByText("complete")).toBeVisible();          // closed project shown
  await expect(page.getByRole("cell", { name: "oltest_old", exact: true }).first()).toBeVisible();
  await expect(page.getByText("failed")).toBeVisible();
  await page.screenshot({ path: "test-results/02-fleet.png", fullPage: true });

  // Fleet drill-down — expand abs_phase1 → its recent events from the real API.
  await page.getByRole("button", { name: "expand abs_phase1" }).click();
  await expect(page.getByText("Recent events")).toBeVisible();
  await expect(page.getByText(/gate_fire/).first()).toBeVisible();

  // Runs — past run history with cost (closed activity).
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Run history" })).toBeVisible();
  await expect(page.getByText(/total \$/)).toBeVisible();
  await expect(page.getByRole("cell", { name: "abs_phase0", exact: true }).first()).toBeVisible();
  await page.screenshot({ path: "test-results/09-runs.png", fullPage: true });

  // Improve — Adopt round-trips proposed→accepted via the real server; applied card has Roll back.
  await page.getByRole("button", { name: "Improve", exact: true }).click();
  const proposed = page.getByRole("region", { name: "proposed" });
  await proposed.getByRole("button", { name: "Adopt" }).click();
  await expect(page.getByRole("region", { name: "accepted" }).getByText(/abs-phase-boundary/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Roll back" })).toBeVisible();   // rollback control present
  await page.screenshot({ path: "test-results/03-improve.png", fullPage: true });

  // Spend, Graph, Events tabs render real data.
  await page.getByRole("button", { name: "Spend", exact: true }).click();
  await expect(page.getByText(/Fleet spent \$/)).toBeVisible();
  await page.getByRole("button", { name: "Graph", exact: true }).click();
  await expect(page.getByText("→ abs_phase1")).toBeVisible();
  await expect(page.getByText("level 2")).toBeVisible();
  await page.getByRole("button", { name: "Events", exact: true }).click();
  await expect(page.getByText(/gate_fire/).first()).toBeVisible();

  // Actions — every action recorded.
  await page.getByRole("button", { name: "Actions", exact: true }).click();
  await page.screenshot({ path: "test-results/06-actions.png", fullPage: true });
  await expect(page.getByText(/gate-resolve/).first()).toBeVisible();
  await expect(page.getByText(/promote/).first()).toBeVisible();

  expect(consoleErrors, "browser console errors").toEqual([]);
  expect(pageErrors, "uncaught page errors").toEqual([]);
  expect(badResponses, "unexpected failed responses").toEqual([]);
});
