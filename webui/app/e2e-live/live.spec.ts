import { test, expect } from "@playwright/test";

// No route mocking: every request hits the real FastAPI server (webui.server.demo) over its seeded
// in-memory registry. Proves the real JSON contract + a full action round-trip through the server.

test("UI renders the real API's data and an Adopt round-trips through the real server", async ({ page }) => {
  await page.goto("/");

  // Home inbox, built by the REAL build_inbox over the seeded registry:
  await expect(page.getByRole("heading", { name: "Needs You" })).toBeVisible();
  await expect(page.getByText(/Gate awaiting decision · oltest_c2/)).toBeVisible();      // paused_gate row
  await expect(page.getByText(/Learning ready .*abs-phase-boundary/)).toBeVisible();      // proposed finding
  await expect(page.getByText(/Adopted learning regressed/)).toBeVisible();               // regressed effect
  await expect(page.getByText(/Chronic correction churn · OLB-07/)).toBeVisible();        // churn

  // Fleet tab shows the real snapshot rows.
  await page.getByRole("button", { name: "Fleet" }).click();
  await expect(page.getByRole("cell", { name: "oltest_c2" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "oltest_d2" })).toBeVisible();

  // Improve tab: the proposed finding sits in the proposed column.
  await page.getByRole("button", { name: "Improve" }).click();
  const proposed = page.getByRole("region", { name: "proposed" });
  await expect(proposed.getByText(/abs-phase-boundary/)).toBeVisible();

  // Adopt → POST /api/findings/.../promote hits the REAL server, which flips the finding to
  // 'accepted'; after the UI refresh the card has moved out of proposed into accepted.
  await proposed.getByRole("button", { name: "Adopt" }).click();
  const accepted = page.getByRole("region", { name: "accepted" });
  await expect(accepted.getByText(/abs-phase-boundary/)).toBeVisible();
  await expect(proposed.getByText(/abs-phase-boundary/)).toHaveCount(0);
});
