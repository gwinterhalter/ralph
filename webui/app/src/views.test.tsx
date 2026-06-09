import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InboxView, ImproveView, EffectsView, SpendView, ActionsView, GraphView } from "./views";
import type { InboxCard, Finding, EffectRow, Forecast, ActionRow, GraphNode, GraphEdge } from "./api";

describe("InboxView", () => {
  it("shows the all-clear when there are no cards", () => {
    render(<InboxView cards={[]} onAction={() => {}} />);
    expect(screen.getByRole("status")).toHaveTextContent("Nothing needs you");
  });

  it("renders a card per signal and routes an action click", async () => {
    const cards: InboxCard[] = [
      { kind: "learning", urgency: 4, title: "Learning ready · g1", subject: "k1",
        detail: "add rule", actions: ["adopt", "reject", "why"], recommended: null },
    ];
    const onAction = vi.fn();
    render(<InboxView cards={cards} onAction={onAction} />);
    expect(screen.getByText("Learning ready · g1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "adopt" }));
    expect(onAction).toHaveBeenCalledWith(cards[0], "adopt");
  });
});

describe("ImproveView", () => {
  it("buckets findings into lifecycle columns and shows the effect on applied cards", () => {
    const findings: Finding[] = [
      { finding_key: "k1", kind: "answerer_dsl_candidate", subject: "g1", status: "proposed",
        recommendation: "add rule", authoring_skill: "cf-spec-writer" },
      { finding_key: "k2", kind: "session_shape", subject: "s", status: "applied", recommendation: "tune" },
    ];
    const effects: EffectRow[] = [
      { finding_key: "k2", outcome: "regressed", before_metric: 0.2, after_metric: 0.9,
        post_adoption_runs: 3 },
    ];
    render(
      <ImproveView findings={findings} effects={effects}
        onPromote={() => {}} onReject={() => {}} onApply={() => {}} />,
    );
    // proposed column has the Adopt action; applied card carries its measured effect
    expect(screen.getByRole("button", { name: "Adopt" })).toBeInTheDocument();
    expect(screen.getByText("effect: regressed")).toBeInTheDocument();
  });
});

describe("SpendView", () => {
  it("shows the fleet rollup + per-project forecast rows and admin actions", () => {
    const forecast: Forecast = {
      projects: [{ project_id: "p1", basis: "per_item", open_work_count: 3, total_spent_usd: "5.00",
        projected_remaining_usd: "10.00", projected_total_usd: "15.00", confidence: 0.8 }],
      fleet_spent_usd: "5.00", fleet_projected_remaining_usd: "10.00", fleet_projected_total_usd: "15.00",
    };
    const provision = vi.fn();
    render(<SpendView forecast={forecast} onProvision={provision} onPrune={() => {}} />);
    expect(screen.getByText(/projected total \$15\.00/)).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "per_item" })).toBeInTheDocument();
  });
});

describe("ActionsView", () => {
  it("lists recorded operator actions", () => {
    const actions: ActionRow[] = [
      { ts: "2026-06-08T10:00:00Z", action: "gate-resolve", target: "gate_request_0012_0000.json",
        by: "greg", detail: "proceed" },
    ];
    render(<ActionsView actions={actions} />);
    expect(screen.getByText(/gate-resolve/)).toBeInTheDocument();
    expect(screen.getByText(/proceed/)).toBeInTheDocument();
  });
});

describe("GraphView", () => {
  it("renders nodes layered by depends_on depth with their edges", () => {
    const nodes: GraphNode[] = [
      { id: "abs_phase0", lifecycle_state: "complete" },
      { id: "abs_phase1", lifecycle_state: "running" },
      { id: "abs_phase2", lifecycle_state: "candidate" },
    ];
    const edges: GraphEdge[] = [
      { from: "abs_phase1", to: "abs_phase0" },
      { from: "abs_phase2", to: "abs_phase1" },
    ];
    render(<GraphView nodes={nodes} edges={edges} />);
    expect(screen.getByText("abs_phase2")).toBeInTheDocument();
    expect(screen.getByText("→ abs_phase1")).toBeInTheDocument();  // edge rendered
    // phase2 (depth 2) sits in a deeper level than phase0 (depth 0)
    const labels = screen.getAllByText(/level \d/).map((e) => e.textContent);
    expect(labels).toContain("level 0");
    expect(labels).toContain("level 2");
  });
});

describe("EffectsView", () => {
  it("rolls up outcomes worst-first", () => {
    render(
      <EffectsView
        effects={[{ finding_key: "k", outcome: "regressed", before_metric: 0.2, after_metric: 0.9, post_adoption_runs: 3 }]}
        byOutcome={{ confirmed: 2, regressed: 1 }}
      />,
    );
    // Match the rollup paragraph specifically (the "N regressed, M confirmed" summary), not the
    // list item that also contains the word "regressed".
    const rollup = screen.getByText(/regressed, \d+ confirmed/);
    expect(rollup.textContent!.indexOf("regressed")).toBeLessThan(rollup.textContent!.indexOf("confirmed"));
  });
});
