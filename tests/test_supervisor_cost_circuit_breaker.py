"""Component tests for the OLB-12 Cost Circuit-Breaker (``supervisor/cost_circuit_breaker.py``).

Covers the OLB-12 predicate (Spec v1.3 §10) — one ``@pytest.mark.unit`` case per
FR-039..FR-043 plus short/empty/Decimal edges — entirely DB-free over in-memory
:class:`IterationObservation` fixtures with a supplied :class:`BreakerConfig` (gate
``olb12-cost-circuit-breaker-build-substrate`` = A). The breaker is a pure anomaly
detector, so every case is a direct call with no port, no database, no file I/O, no
wall-clock read, and no live ``set_lifecycle_state``. The §2.3 / §10.1 no-dollar-cap
invariant is asserted mechanically.

Each FR fixture is crafted so exactly one detector fires (or none, for the negative
cases): the spend-delta detector needs a full trailing window of *preceding*
iterations, so the FR-040/FR-041 fixtures use short histories or a decreasing
open-count to keep the higher-precedence detectors silent and isolate the rule under
test.
"""
from __future__ import annotations

import dataclasses
import inspect
from decimal import Decimal

import pytest

from supervisor import cost_circuit_breaker as ccb
from supervisor.cost_circuit_breaker import (
    BreakerConfig,
    BreakerTrip,
    CostSummary,
    IterationObservation,
    TripKind,
    evaluate,
    evaluate_fleet,
    summarize_cost,
    trailing_median_spend,
)
from supervisor.safety_gates import ESCALATION_TIER_TOP, PAUSED_SAFETY

# A fixed config — every threshold the detectors key on is supplied here (the module
# reads no seed). spend_delta_multiple 3x over a trailing window of 3; K=J=3.
CONFIG = BreakerConfig(
    spend_delta_multiple=Decimal(3),
    trailing_window=3,
    spend_without_closure_k=3,
    target_loop_j=3,
)


def _obs(
    *,
    project_id: str = "proj-a",
    index: int = 0,
    spend: str = "10",
    open_count: int = 5,
    target_id: str | None = None,
    target_closed: bool = False,
) -> IterationObservation:
    """Build an :class:`IterationObservation` with defaults for fields not under test."""
    return IterationObservation(
        project_id=project_id,
        iteration_index=index,
        spend_usd=Decimal(spend),
        open_count=open_count,
        target_id=target_id,
        target_closed=target_closed,
    )


# --- FR-039: single-iteration spend-delta anomaly ----------------------------


@pytest.mark.unit
def test_fr039_spend_delta_anomaly_trips() -> None:
    """FR-039: a latest iteration spending more than ``multiple`` x the trailing median
    trips ``SPEND_DELTA_ANOMALY`` with a ``paused_safety`` top-tier decision; a spend
    within the threshold does not trip."""
    # Trailing median over the 3 preceding iterations is 10; latest spends 31 > 3*10.
    # open_count decreases (no FR-040) and target is None (no FR-041) — only FR-039.
    base = [
        _obs(index=0, spend="10", open_count=5),
        _obs(index=1, spend="10", open_count=4),
        _obs(index=2, spend="10", open_count=3),
    ]
    tripped = evaluate(base + [_obs(index=3, spend="31", open_count=2)], CONFIG)
    assert tripped.tripped is True
    assert tripped.kind is TripKind.SPEND_DELTA_ANOMALY
    assert tripped.project_id == "proj-a"
    assert tripped.escalation is not None
    assert tripped.escalation.lifecycle_state == PAUSED_SAFETY
    assert tripped.escalation.tier == ESCALATION_TIER_TOP
    assert tripped.escalation.killed is False

    # Exactly 3x the median (30) is within threshold (strictly-greater test) — no trip.
    within = evaluate(base + [_obs(index=3, spend="30", open_count=2)], CONFIG)
    assert within.tripped is False
    assert within.kind is None


# --- FR-040: spend accruing without any closure ------------------------------


@pytest.mark.unit
def test_fr040_spend_without_closure_trips() -> None:
    """FR-040: open work-count not decreasing across K iterations while spend accrues
    trips ``SPEND_WITHOUT_CLOSURE``; a window where the open-count decreases does not."""
    # 3 iterations, open held at 5 while spending each time, no single target (no
    # FR-041); only 3 records so FR-039 lacks its trailing window — isolates FR-040.
    stalled = [
        _obs(index=0, spend="10", open_count=5),
        _obs(index=1, spend="10", open_count=5),
        _obs(index=2, spend="10", open_count=5),
    ]
    tripped = evaluate(stalled, CONFIG)
    assert tripped.tripped is True
    assert tripped.kind is TripKind.SPEND_WITHOUT_CLOSURE
    assert tripped.escalation is not None
    assert tripped.escalation.lifecycle_state == PAUSED_SAFETY

    # open-count decreases within the window (progress) — no trip.
    progressing = [
        _obs(index=0, spend="10", open_count=5),
        _obs(index=1, spend="10", open_count=4),
        _obs(index=2, spend="10", open_count=3),
    ]
    assert evaluate(progressing, CONFIG).tripped is False


# --- FR-041: same target re-attempted without closing ------------------------


@pytest.mark.unit
def test_fr041_target_loop_trips_and_names_target() -> None:
    """FR-041: the same target re-attempted across J iterations without closing trips
    ``TARGET_LOOP`` and names the looping target; a target that closes does not trip."""
    # Same target across 3 iterations, never closed. open_count decreases so FR-040
    # stays silent, and only 3 records so FR-039 lacks its trailing window.
    looping = [
        _obs(index=0, spend="10", open_count=5, target_id="OLB-99"),
        _obs(index=1, spend="10", open_count=4, target_id="OLB-99"),
        _obs(index=2, spend="10", open_count=3, target_id="OLB-99"),
    ]
    tripped = evaluate(looping, CONFIG)
    assert tripped.tripped is True
    assert tripped.kind is TripKind.TARGET_LOOP
    assert tripped.looping_target == "OLB-99"
    assert tripped.escalation is not None
    assert tripped.escalation.lifecycle_state == PAUSED_SAFETY

    # The target closes within the window — no loop, no trip.
    closing = [
        _obs(index=0, spend="10", open_count=5, target_id="OLB-99"),
        _obs(index=1, spend="10", open_count=4, target_id="OLB-99"),
        _obs(index=2, spend="10", open_count=3, target_id="OLB-99", target_closed=True),
    ]
    assert evaluate(closing, CONFIG).tripped is False


# --- FR-042: non-binding cost surfacing (no dollar cap) ----------------------


@pytest.mark.unit
def test_fr042_cost_surfacing_is_non_binding() -> None:
    """FR-042 / §2.3: ``summarize_cost`` surfaces cumulative + per-iteration figures,
    and a large cumulative spend with healthy *shape* never trips — there is no
    dollar-cap path anywhere in the module."""
    # Large per-iteration spend, but healthy shape: open-count decreasing (progress),
    # spend flat (no delta blow-out), no looping target.
    healthy = [
        _obs(index=0, spend="1000", open_count=5),
        _obs(index=1, spend="1000", open_count=4),
        _obs(index=2, spend="1000", open_count=3),
        _obs(index=3, spend="1000", open_count=2),
    ]
    assert evaluate(healthy, CONFIG).tripped is False  # high cost, no anomaly => no trip

    summary = summarize_cost(healthy)
    assert summary.cumulative_usd == Decimal(4000)
    assert summary.per_iteration_usd == (Decimal(1000),) * 4
    assert summary.project_id == "proj-a"

    # Mechanically: no field/parameter is a dollar cap anywhere in the module.
    cap_names = ("cap", "max_usd", "budget_limit", "limit", "ceiling", "halt")
    config_fields = {f.name for f in dataclasses.fields(BreakerConfig)}
    assert not any(any(c in name for c in cap_names) for name in config_fields)
    for _, fn in inspect.getmembers(ccb, inspect.isfunction):
        params = inspect.signature(fn).parameters
        assert not any(any(c in p for c in cap_names) for p in params)


# --- FR-043: per-Project trip isolation --------------------------------------


@pytest.mark.unit
def test_fr043_trip_isolation() -> None:
    """FR-043: a trip on one Project never affects another — ``evaluate_fleet`` trips
    only the pathological Project, leaving the healthy one clean."""
    pathological = [
        _obs(project_id="bad", index=0, spend="10", open_count=5),
        _obs(project_id="bad", index=1, spend="10", open_count=4),
        _obs(project_id="bad", index=2, spend="10", open_count=3),
        _obs(project_id="bad", index=3, spend="31", open_count=2),  # FR-039 blow-out
    ]
    healthy = [
        _obs(project_id="good", index=0, spend="10", open_count=5),
        _obs(project_id="good", index=1, spend="10", open_count=4),
        _obs(project_id="good", index=2, spend="10", open_count=3),
        _obs(project_id="good", index=3, spend="10", open_count=2),
    ]
    results = evaluate_fleet({"bad": pathological, "good": healthy}, CONFIG)
    assert results["bad"].tripped is True
    assert results["bad"].kind is TripKind.SPEND_DELTA_ANOMALY
    assert results["good"].tripped is False
    assert results["good"].project_id == "good"


# --- Edge cases --------------------------------------------------------------


@pytest.mark.unit
def test_short_history_no_false_trip() -> None:
    """A history shorter than the trailing window / K / J trips nothing and raises
    nothing (no divide-by-zero against an empty/zero baseline)."""
    short = [
        _obs(index=0, spend="10", open_count=5, target_id="OLB-1"),
        _obs(index=1, spend="9999", open_count=5, target_id="OLB-1"),
    ]
    assert evaluate(short, CONFIG).tripped is False
    assert ccb.detect_spend_delta_anomaly(short, CONFIG) is None
    assert ccb.detect_spend_without_closure(short, CONFIG) is None
    assert ccb.detect_target_loop(short, CONFIG) is None


@pytest.mark.unit
def test_empty_history_returns_no_trip() -> None:
    """An empty history yields a clean no-trip and a zero cost summary, no exception."""
    trip = evaluate([], CONFIG)
    assert isinstance(trip, BreakerTrip)
    assert trip.tripped is False
    assert trip.kind is None

    summary = summarize_cost([])
    assert summary == CostSummary(project_id="", cumulative_usd=Decimal(0))
    assert summary.per_iteration_usd == ()

    assert evaluate_fleet({}, CONFIG) == {}


@pytest.mark.unit
def test_money_is_decimal_not_float() -> None:
    """NFR-007: spend figures and the surfaced totals are :class:`Decimal`, never float."""
    history = [_obs(index=0, spend="1.05"), _obs(index=1, spend="2.10")]
    for obs in history:
        assert isinstance(obs.spend_usd, Decimal)
        assert not isinstance(obs.spend_usd, float)

    summary = summarize_cost(history)
    assert isinstance(summary.cumulative_usd, Decimal)
    assert summary.cumulative_usd == Decimal("3.15")
    assert all(isinstance(v, Decimal) for v in summary.per_iteration_usd)

    median = trailing_median_spend(history, window=2)
    assert isinstance(median, Decimal)
    assert median == Decimal("1.575")  # (1.05 + 2.10) / 2, exact in Decimal


@pytest.mark.unit
def test_trailing_median_even_and_odd_windows() -> None:
    """``trailing_median_spend`` averages the two middle values for an even window and
    returns the middle one for an odd window; a degenerate window yields zero."""
    odd = [_obs(spend="10"), _obs(spend="30"), _obs(spend="20")]
    assert trailing_median_spend(odd, window=3) == Decimal(20)
    even = [_obs(spend="10"), _obs(spend="20"), _obs(spend="30"), _obs(spend="40")]
    assert trailing_median_spend(even, window=4) == Decimal(25)
    assert trailing_median_spend([], window=3) == Decimal(0)
    assert trailing_median_spend(odd, window=0) == Decimal(0)
