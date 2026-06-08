import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InboxView, ImproveView, EffectsView } from "./views";
import type { InboxCard, Finding, EffectRow } from "./api";

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
