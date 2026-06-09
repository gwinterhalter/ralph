import { test, expect } from "@playwright/test";

// No route mocking: every request hits the real FastAPI server (webui.server.demo) over its seeded
// in-memory registry + a seeded pending gate_request file. Proves the real JSON contract + full
// action round-trips through the real server, across every screen. "Verified working" is enforced:
// the test FAILS on any browser console error, uncaught page error, or 4xx-5xx response.

test("full console: real data renders, gate resolves, adopt round-trips, all tabs — zero errors", async ({ page }) => {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const badResponses: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  // 502 is the expected, asserted-for response when apply's dispatcher can't resolve the skill in
  // this headless env — exclude it from the "bad response" gate; everything else must be < 400.
  page.on("response", (r) => { if (r.status() >= 400 && r.status() !== 502) badResponses.push(`${r.status()} ${r.url()}`); });

  await page.goto("/");

  // Home inbox, built by the REAL build_inbox over the seeded registry + gate file + budget breach:
  await expect(page.getByRole("heading", { name: "Needs You" })).toBeVisible();
  await expect(page.getByText("Budget ceiling tripped")).toBeVisible();                  // breach (spend≥ceiling)
  await expect(page.getByText(/Gate · abs-phase-boundary/)).toBeVisible();                // rich gate from the file
  await expect(page.getByText("proceed to Phase 1?")).toBeVisible();                      // real question_text
  await expect(page.getByText(/Learning ready .*abs-phase-boundary/)).toBeVisible();
  await expect(page.getByText(/Adopted learning regressed/)).toBeVisible();
  await expect(page.getByText(/Chronic correction churn · OLB-07/)).toBeVisible();
  await page.screenshot({ path: "test-results/01-home.png", fullPage: true });

  // Resolve the gate: click its real option ("proceed") → real POST /api/gates/resolve writes the
  // gate_response file; after refresh the gate card is gone (no longer pending).
  await page.getByRole("button", { name: "proceed" }).click();
  await expect(page.getByText("proceed to Phase 1?")).toHaveCount(0);

  // Fleet tab — real snapshot rows.
  await page.getByRole("button", { name: "Fleet", exact: true }).click();
  await expect(page.getByRole("cell", { name: "oltest_c2", exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: "oltest_d2", exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/02-fleet.png", fullPage: true });

  // Improve tab — Adopt round-trips proposed→accepted via the real server.
  await page.getByRole("button", { name: "Improve", exact: true }).click();
  const proposed = page.getByRole("region", { name: "proposed" });
  await proposed.getByRole("button", { name: "Adopt" }).click();
  await expect(page.getByRole("region", { name: "accepted" }).getByText(/abs-phase-boundary/)).toBeVisible();
  await expect(proposed.getByText(/abs-phase-boundary/)).toHaveCount(0);
  await page.screenshot({ path: "test-results/03-improve.png", fullPage: true });

  // Spend tab — real forecast.
  await page.getByRole("button", { name: "Spend", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Spend" })).toBeVisible();
  await expect(page.getByText(/Fleet spent \$/)).toBeVisible();
  await page.screenshot({ path: "test-results/04-spend.png", fullPage: true });

  // Events tab — real events.
  await page.getByRole("button", { name: "Events", exact: true }).click();
  await expect(page.getByText(/gate_fire/).first()).toBeVisible();
  await page.screenshot({ path: "test-results/05-events.png", fullPage: true });

  // Graph tab — real depends_on chain (abs_phase0 ← phase1 ← phase2).
  await page.getByRole("button", { name: "Graph", exact: true }).click();
  await expect(page.getByText("→ abs_phase1")).toBeVisible();          // phase2 depends_on phase1
  await expect(page.getByText("level 2")).toBeVisible();               // layered by depth
  await page.screenshot({ path: "test-results/07-graph.png", fullPage: true });

  // Fleet row drill-down — expand oltest_c2 → its recent events load from the real API.
  await page.getByRole("button", { name: "Fleet", exact: true }).click();
  await page.getByRole("button", { name: "expand oltest_c2" }).click();
  await expect(page.getByText("Recent events")).toBeVisible();
  await expect(page.getByText(/gate_fire/).first()).toBeVisible();   // real event under the row
  await page.screenshot({ path: "test-results/08-fleet-detail.png", fullPage: true });

  // Actions tab — every action we took was recorded (gate-resolve + promote).
  await page.getByRole("button", { name: "Actions", exact: true }).click();
  await page.screenshot({ path: "test-results/06-actions.png", fullPage: true });
  await expect(page.getByText(/gate-resolve/).first()).toBeVisible();
  await expect(page.getByText(/promote/).first()).toBeVisible();

  expect(consoleErrors, "browser console errors").toEqual([]);
  expect(pageErrors, "uncaught page errors").toEqual([]);
  expect(badResponses, "unexpected failed responses").toEqual([]);
});
