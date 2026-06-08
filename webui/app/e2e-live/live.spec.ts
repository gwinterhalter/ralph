import { test, expect } from "@playwright/test";

// No route mocking: every request hits the real FastAPI server (webui.server.demo) over its seeded
// in-memory registry. Proves the real JSON contract + a full action round-trip through the server.
//
// "Verified working" is enforced, not assumed: we attach listeners so the test FAILS on any browser
// console error, uncaught page error, or failed/4xx-5xx network response — a passing run therefore
// means the UI rendered and operated with zero runtime errors. Screenshots are captured for visual
// proof (test-results/, gitignored).

test("UI renders the real API's data and an Adopt round-trips — with zero runtime errors", async ({ page }) => {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const badResponses: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  page.on("response", (r) => { if (r.status() >= 400) badResponses.push(`${r.status()} ${r.url()}`); });

  await page.goto("/");

  // Home inbox, built by the REAL build_inbox over the seeded registry:
  await expect(page.getByRole("heading", { name: "Needs You" })).toBeVisible();
  await expect(page.getByText(/Gate awaiting decision · oltest_c2/)).toBeVisible();      // paused_gate row
  await expect(page.getByText(/Learning ready .*abs-phase-boundary/)).toBeVisible();      // proposed finding
  await expect(page.getByText(/Adopted learning regressed/)).toBeVisible();               // regressed effect
  await expect(page.getByText(/Chronic correction churn · OLB-07/)).toBeVisible();        // churn
  await page.screenshot({ path: "test-results/01-home.png", fullPage: true });

  // Fleet tab shows the real snapshot rows.
  await page.getByRole("button", { name: "Fleet" }).click();
  await expect(page.getByRole("cell", { name: "oltest_c2" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "oltest_d2" })).toBeVisible();
  await page.screenshot({ path: "test-results/02-fleet.png", fullPage: true });

  // Improve tab: the proposed finding sits in the proposed column.
  await page.getByRole("button", { name: "Improve" }).click();
  const proposed = page.getByRole("region", { name: "proposed" });
  await expect(proposed.getByText(/abs-phase-boundary/)).toBeVisible();
  await page.screenshot({ path: "test-results/03-improve-before.png", fullPage: true });

  // Adopt → POST /api/findings/.../promote hits the REAL server, which flips the finding to
  // 'accepted'; after the UI refresh the card has moved out of proposed into accepted.
  await proposed.getByRole("button", { name: "Adopt" }).click();
  const accepted = page.getByRole("region", { name: "accepted" });
  await expect(accepted.getByText(/abs-phase-boundary/)).toBeVisible();
  await expect(proposed.getByText(/abs-phase-boundary/)).toHaveCount(0);
  await page.screenshot({ path: "test-results/04-improve-after-adopt.png", fullPage: true });

  // Effects tab: the regressed adoption renders with its before→after metric.
  await page.getByRole("button", { name: "Effects" }).click();
  await expect(page.getByText(/session_shape:spec_review_loop/)).toBeVisible();
  await expect(page.getByText(/0\.200 → 0\.900/)).toBeVisible();
  await page.screenshot({ path: "test-results/05-effects.png", fullPage: true });

  // Zero-runtime-error gate (covers the 5s poll loop that fired during the run too).
  expect(consoleErrors, "browser console errors").toEqual([]);
  expect(pageErrors, "uncaught page errors").toEqual([]);
  expect(badResponses, "failed/4xx-5xx responses").toEqual([]);
});
