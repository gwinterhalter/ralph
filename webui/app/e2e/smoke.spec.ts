import { test, expect } from "@playwright/test";

// API responses are mocked so the smoke verifies the real browser wiring (render → click → POST)
// without a Python server or DB. Each route returns a minimal but realistic payload.
test.beforeEach(async ({ page }) => {
  await page.route("**/api/inbox", (r) =>
    r.fulfill({
      json: {
        count: 2,
        cards: [
          { kind: "gate", urgency: 1, title: "Gate awaiting decision · oltest_c2", subject: "oltest_c2",
            detail: "a Project is paused on a gate", actions: ["proceed", "hold", "details"], recommended: null },
          { kind: "learning", urgency: 4, title: "Learning ready · answerer g1", subject: "answerer_dsl_candidate:g1",
            detail: "add rule → cf-spec-writer", actions: ["adopt", "reject", "why"], recommended: null },
        ],
      },
    }),
  );
  await page.route("**/api/fleet", (r) =>
    r.fulfill({
      json: {
        as_of: "2026-06-08T00:00:00+00:00", rows: [], running_count: 0, concurrency_ceiling: 3,
        headroom: 3, stalled_count: 0, total_cumulative_cost_usd: "0",
      },
    }),
  );
  await page.route("**/api/learnings*", (r) => r.fulfill({ json: { findings: [], by_status: {} } }));
  await page.route("**/api/effects", (r) => r.fulfill({ json: { effects: [], by_outcome: {} } }));
  await page.route("**/api/forecast", (r) =>
    r.fulfill({ json: { projects: [], fleet_spent_usd: "0", fleet_projected_remaining_usd: "0", fleet_projected_total_usd: "0" } }),
  );
  await page.route("**/api/events*", (r) => r.fulfill({ json: { events: [], metrics: { total: 0, failures: 0 } } }));
  await page.route("**/api/actions", (r) => r.fulfill({ json: { actions: [] } }));
  await page.route("**/api/graph", (r) => r.fulfill({ json: { nodes: [], edges: [] } }));
  await page.route("**/api/stream*", (r) =>
    r.fulfill({ headers: { "content-type": "text/event-stream" }, body: "data: {}\n\n" }),
  );
});

test("home renders the needs-you inbox and Adopt fires a promote POST", async ({ page }) => {
  let promoted = "";
  await page.route("**/api/findings/*/promote", (route) => {
    promoted = route.request().url();
    return route.fulfill({ json: { finding_key: "answerer_dsl_candidate:g1", status: "accepted" } });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Needs You" })).toBeVisible();
  await expect(page.getByText("Gate awaiting decision · oltest_c2")).toBeVisible();
  await expect(page.getByText("Learning ready · answerer g1")).toBeVisible();

  // The Home tab badge reflects the card count.
  await expect(page.getByRole("button", { name: /Home \(2\)/ })).toBeVisible();

  // Adopt on the learning card → POST /api/findings/.../promote
  await page.getByRole("button", { name: "adopt" }).click();
  await expect.poll(() => promoted).toContain("/promote");
});
