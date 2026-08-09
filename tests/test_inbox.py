"""Operator Action-Inbox aggregation (supervisor.inbox) — real-seam: substrate rows → cards."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from supervisor.full_status_surface import (
    HEARTBEAT_HEALTHY,
    HEARTBEAT_NA,
    HEARTBEAT_STALLED,
    ProjectFullStatusRow,
)
from supervisor.inbox import (
    KIND_APPROVAL,
    KIND_BUDGET,
    KIND_CHURN,
    KIND_FAILED,
    KIND_GATE,
    KIND_LEARNING,
    KIND_REGRESSED,
    KIND_STALL,
    build_inbox,
)

pytestmark = pytest.mark.unit


def _row(pid: str, *, lifecycle: str, heartbeat: str = HEARTBEAT_NA) -> ProjectFullStatusRow:
    return ProjectFullStatusRow(
        project_id=pid,
        display_name=pid,
        lifecycle_state=lifecycle,
        active_run_status="running" if heartbeat != HEARTBEAT_NA else "—",
        attention_debt=0,
        open_work_count=1,
        cumulative_cost_usd=Decimal(0),
        heartbeat_state=heartbeat,
        heartbeat_as_of=datetime(2026, 6, 8, tzinfo=UTC),
    )


def test_clean_fleet_yields_empty_inbox() -> None:
    rows = [_row("p1", lifecycle="running", heartbeat=HEARTBEAT_HEALTHY)]
    findings = [{"finding_key": "k", "status": "applied"}]  # not proposed
    effects = [{"finding_key": "k", "outcome": "confirmed"}]  # confirmed
    assert build_inbox(fleet_rows=rows, findings=findings, effects=effects) == []


def test_gate_and_stall_from_fleet_rows() -> None:
    rows = [
        _row("pg", lifecycle="paused_gate"),
        _row("ps", lifecycle="running", heartbeat=HEARTBEAT_STALLED),
    ]
    cards = build_inbox(fleet_rows=rows)
    by_kind = {c.kind: c for c in cards}
    assert by_kind[KIND_GATE].subject == "pg"
    assert "proceed" in by_kind[KIND_GATE].actions
    assert by_kind[KIND_STALL].subject == "ps"
    assert "force_reap" in by_kind[KIND_STALL].actions


def test_proposed_findings_become_learning_cards_only() -> None:
    findings = [
        {"finding_key": "k1", "kind": "answerer_dsl_candidate", "subject": "g1",
         "status": "proposed", "recommendation": "add rule", "authoring_skill": "cf-spec-writer"},
        {"finding_key": "k2", "kind": "session_shape", "subject": "s", "status": "applied"},
    ]
    cards = build_inbox(findings=findings)
    assert len(cards) == 1
    assert cards[0].kind == KIND_LEARNING and cards[0].subject == "k1"
    assert "cf-spec-writer" in cards[0].detail


def test_non_confirmed_effects_become_regressed_cards() -> None:
    effects = [
        {"finding_key": "kr", "outcome": "regressed", "before_metric": 0.2, "after_metric": 0.9,
         "post_adoption_runs": 3},
        {"finding_key": "kn", "outcome": "no_effect", "before_metric": 1.0, "after_metric": 1.0,
         "post_adoption_runs": 4},
        {"finding_key": "kc", "outcome": "confirmed", "before_metric": 1.0, "after_metric": 0.0,
         "post_adoption_runs": 3},
    ]
    cards = build_inbox(effects=effects)
    subjects = {c.subject for c in cards if c.kind == KIND_REGRESSED}
    assert subjects == {"kr", "kn"}  # confirmed excluded
    assert any("0.200 → 0.900" in c.detail for c in cards)
    assert all("revert" in c.actions for c in cards)


def test_pending_gates_become_rich_gate_cards_and_dedupe_lifecycle() -> None:
    rows = [_row("pg", lifecycle="paused_gate")]  # would emit a generic gate card...
    gates = [{
        "request_file": "gate_request_0012_0000.json", "gate_id": "abs-phase-boundary",
        "project_id": "pg", "question_text": "proceed to Phase 1?",
        "options": [{"id": "proceed", "label": "Proceed"}, {"id": "hold", "label": "Hold"}],
    }]
    cards = build_inbox(fleet_rows=rows, gates=gates)
    gate_cards = [c for c in cards if c.kind == KIND_GATE]
    # ...but the rich gate (same project_id) dedupes the generic lifecycle card.
    assert len(gate_cards) == 1
    g = gate_cards[0]
    assert g.subject == "gate_request_0012_0000.json"
    assert g.detail == "proceed to Phase 1?"
    assert g.actions == ("proceed", "hold", "details")


def test_lifecycle_gate_card_kept_when_no_request_file() -> None:
    rows = [_row("pg", lifecycle="paused_gate")]
    cards = build_inbox(fleet_rows=rows, gates=[])  # no surfaced request file
    assert [c.kind for c in cards] == [KIND_GATE]
    assert cards[0].subject == "pg"


def test_paused_gate_project_surfaces_even_when_absent_from_fleet() -> None:
    # The live-found defect: a paused_gate project is neither candidate nor running (not in the
    # snapshot) and has no gate file -> it must STILL raise an attention card.
    projects = [{"project_id": "oltest_c2", "display_name": "oltest_c2", "lifecycle_state": "paused_gate"}]
    cards = build_inbox(fleet_rows=[], projects=projects)
    assert len(cards) == 1 and cards[0].kind == KIND_GATE
    assert cards[0].subject == "oltest_c2" and "investigate" in cards[0].actions


def test_paused_gate_not_double_counted_across_sources() -> None:
    # same project in projects AND fleet AND a file -> exactly one gate card
    projects = [{"project_id": "pg", "lifecycle_state": "paused_gate"}]
    rows = [_row("pg", lifecycle="paused_gate")]
    gates = [{"request_file": "gate_request_1_0.json", "gate_id": "g", "project_id": "pg",
              "question_text": "q?", "options": [{"id": "yes"}]}]
    cards = build_inbox(fleet_rows=rows, projects=projects, gates=gates)
    assert len([c for c in cards if c.kind == KIND_GATE]) == 1


def test_failed_project_surfaces_as_attention() -> None:
    projects = [{"project_id": "pf", "lifecycle_state": "failed"}]
    cards = build_inbox(projects=projects)
    assert len(cards) == 1 and cards[0].kind == KIND_FAILED and cards[0].subject == "pf"


def test_pending_approval_project_surfaces_as_approval_card() -> None:
    projects = [{"project_id": "proposed_x", "display_name": "Proposed X",
                 "lifecycle_state": "pending_approval"}]
    cards = build_inbox(projects=projects)
    assert len(cards) == 1 and cards[0].kind == KIND_APPROVAL and cards[0].subject == "proposed_x"
    assert "approve" in cards[0].actions and "reject" in cards[0].actions


def test_churn_threshold() -> None:
    corrections = [
        {"item_id": "OLB-07", "attempts": 5, "projects": 2, "max_level": "L4"},
        {"item_id": "OLB-12", "attempts": 2, "projects": 1, "max_level": "L2"},
    ]
    cards = build_inbox(corrections=corrections, churn_threshold=3)
    churn = [c for c in cards if c.kind == KIND_CHURN]
    assert len(churn) == 1 and churn[0].subject == "OLB-07"


def test_priority_order_budget_first_then_gate() -> None:
    rows = [_row("pg", lifecycle="paused_gate")]
    findings = [{"finding_key": "k1", "status": "proposed", "kind": "x", "subject": "s"}]
    cards = build_inbox(
        fleet_rows=rows,
        findings=findings,
        budget_breach={"project_id": "*fleet*", "detail": "spend > ceiling"},
    )
    assert cards[0].kind == KIND_BUDGET
    assert cards[1].kind == KIND_GATE
    assert cards[-1].kind == KIND_LEARNING
    # urgency is monotonic non-decreasing
    assert [c.urgency for c in cards] == sorted(c.urgency for c in cards)
