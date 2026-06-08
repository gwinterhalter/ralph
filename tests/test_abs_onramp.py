"""ABS-phase on-ramp plan (supervisor.abs_onramp)."""
from __future__ import annotations

import pytest

from supervisor.abs_onramp import abs_chain_plan, render_chain_plan

pytestmark = pytest.mark.unit


def test_chain_plan_edges_and_priorities() -> None:
    phases = {p.project_id: p for p in abs_chain_plan()}
    assert set(phases) == {"abs_phase0", "abs_phase1", "abs_phase2"}
    # The dependency chain (Item 1 gate): each phase depends on the prior.
    assert phases["abs_phase0"].depends_on == ()
    assert phases["abs_phase1"].depends_on == ("abs_phase0",)
    assert phases["abs_phase2"].depends_on == ("abs_phase1",)
    # Priority descends along the chain so the unblocked-next phase is preferred.
    assert phases["abs_phase0"].priority > phases["abs_phase1"].priority > phases["abs_phase2"].priority


def test_render_chain_plan() -> None:
    out = render_chain_plan(abs_chain_plan())
    assert "ABS on-ramp plan" in out
    assert "abs_phase1" in out and "depends_on=abs_phase0" in out
    assert "Item 1 holds each phase until its prerequisite is `complete`" in out
