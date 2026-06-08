"""Item 2 — live assembly for the §4.4 step-6 Learn pass (supervisor.learn_assembly)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from supervisor.attention import auto_pick_eligible
from supervisor.learn_assembly import (
    LearningRecord,
    RunFacts,
    assemble_run_facts,
    build_binding_outcomes,
    build_correction_attempts,
    build_gate_events,
    build_shape_usages,
    completed_run_records,
    findings_to_escalations,
    gate_events_from_run,
    learning_records,
    read_events_jsonl,
    render_learning_corpus,
    run_facts_from_run,
)
from supervisor.run_auditor import (
    AuditConfig,
    AuditFinding,
    BindingFindingClass,
    CorrectionRecord,
    FindingKind,
    GateEvent,
    RunRecord,
    derive_correction_findings,
    run_audit_pass,
)

pytestmark = pytest.mark.unit


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "run_id": "r1",
        "project_id": "p1",
        "status": "complete",
        "terminal_cost_usd": Decimal("1.00"),
        "spawned_at": "2026-06-01T00:00:00+00:00",
        "terminated_at": "2026-06-01T00:10:00+00:00",
        "metadata": {},
    }
    base.update(kw)
    return base


def test_completed_run_records_maps_terminal_rows() -> None:
    records = completed_run_records([_row(run_id="a", status="complete"), _row(run_id="b", status="failed")])
    assert [r.run_id for r in records] == ["a", "b"]
    assert all(isinstance(r, RunRecord) for r in records)
    # Facts are empty until the OLB-16 event-stream assembly (no fabrication).
    assert records[0].gate_events == ()
    assert records[0].binding_outcomes == ()
    assert records[0].shape_usages == ()


def test_completed_run_records_skips_non_terminal() -> None:
    records = completed_run_records([_row(status="running"), _row(run_id="ok", status="complete")])
    assert [r.run_id for r in records] == ["ok"]


def test_learning_records_cost_and_duration() -> None:
    records = learning_records([_row(run_id="a", terminal_cost_usd=Decimal("2.50"))])
    assert len(records) == 1
    rec = records[0]
    assert rec.run_id == "a"
    assert rec.project_slug == "p1"
    assert rec.cost_usd == Decimal("2.50")
    assert rec.duration_seconds == 600.0  # 10 minutes


def test_learning_records_tolerates_missing_fields() -> None:
    # No cost, unparseable / missing timestamps → None, never raises.
    records = learning_records(
        [_row(terminal_cost_usd=None, spawned_at=None, terminated_at="nonsense")]
    )
    assert records[0].cost_usd is None
    assert records[0].duration_seconds is None


def test_render_learning_corpus_is_deterministic_jsonl() -> None:
    records = [
        LearningRecord(run_id="b", project_slug="p", status="failed", cost_usd=None, duration_seconds=None),
        LearningRecord(run_id="a", project_slug="p", status="complete", cost_usd=Decimal("1.25"), duration_seconds=600.0),
    ]
    text = render_learning_corpus(records)
    lines = text.splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["run_id"] == "a"  # sorted by run_id
    assert first["cost_usd"] == "1.25"  # exact-decimal string (NFR-007)
    assert first["duration_seconds"] == 600.0
    second = json.loads(lines[1])
    assert second["run_id"] == "b"
    assert second["cost_usd"] is None
    # Idempotent: re-rendering the same set yields identical text.
    assert render_learning_corpus(records) == text


def test_render_learning_corpus_empty() -> None:
    assert render_learning_corpus([]) == ""


# --- Event-stream gate-fact assembly (FR-050) ---------------------------------


def _ev(event_type: str, gate_id: str, **payload: object) -> dict[str, object]:
    return {
        "event_type": event_type,
        "subject_id": gate_id,
        "project_id": "p1",
        "ts_utc": "2026-06-07T10:01:00.000Z",
        "payload": {"gate_id": gate_id, **payload},
    }


def test_build_gate_events_escalation_and_option() -> None:
    events = [
        _ev("gate_fire", "g1", cls="gate_human", cluster="auto-resolve"),
        _ev("gate_resolve", "g1", mode="inline", via="auto_resolve", option="optA"),
        _ev("gate_fire", "g2", cls="gate_dc"),  # not a human gate
        _ev("gate_escalate", "g3", awaiting="operator"),
        _ev("gate_resolve", "g3", mode="operator"),  # operator-resolved, NO option recorded
        _ev("iteration_start", ""),  # non-gate, ignored
        {  # an audit_target event whose subject_id is a doc name — must NOT become a "gate"
            "event_type": "audit_target_enter",
            "subject_id": "SomeDoc_v1.0",
            "subject_kind": "audit_target",
            "project_id": "p1",
            "ts_utc": "2026-06-07T10:02:00.000Z",
            "payload": {},
        },
    ]
    by_id = {g.gate_id: g for g in build_gate_events(events)}

    assert by_id["g1"].escalated_to_gate_human is True
    assert by_id["g1"].resolved_option == "optA"
    assert by_id["g2"].escalated_to_gate_human is False  # gate_dc, not human
    assert by_id["g3"].escalated_to_gate_human is True
    assert by_id["g3"].resolved_option is None  # no option captured → FR-050 ignores it
    assert "" not in by_id  # the empty-gate non-gate event contributed nothing
    assert "SomeDoc_v1.0" not in by_id  # a non-gate subject_id is never mistaken for a gate


def test_completed_run_records_threads_facts() -> None:
    def _facts(_row: object) -> RunFacts:
        return RunFacts(
            gate_events=(GateEvent(gate_id="g1", escalated_to_gate_human=True, resolved_option="A"),)
        )

    records = completed_run_records([_row(run_id="a")], facts_for=_facts)
    assert records[0].gate_events == (
        GateEvent(gate_id="g1", escalated_to_gate_human=True, resolved_option="A"),
    )
    # Default (no callable) leaves all fact collections empty (back-compat).
    bare = completed_run_records([_row()])[0]
    assert bare.gate_events == () and bare.binding_outcomes == () and bare.shape_usages == ()


def test_fr050_finding_fires_over_assembled_gate_events() -> None:
    """End-to-end: a gate escalated + resolved with the SAME option across 3 Runs (the assembled
    facts) yields an Answerer-DSL candidate — Layer-2 learnings now light up."""
    def _facts(_row: object) -> RunFacts:
        return RunFacts(
            gate_events=(GateEvent(gate_id="recurring-gate", escalated_to_gate_human=True, resolved_option="B"),)
        )

    rows = [_row(run_id="r1"), _row(run_id="r2"), _row(run_id="r3")]
    records = completed_run_records(rows, facts_for=_facts)
    report = run_audit_pass(records, config=AuditConfig(min_consistent_runs=3))

    dsl = [f for f in report.findings if f.kind is FindingKind.ANSWERER_DSL_CANDIDATE]
    assert len(dsl) == 1
    assert dsl[0].subject == "recurring-gate"
    assert "B" in dsl[0].recommendation


# --- FR-051 binding + FR-052 shape fact assembly ------------------------------


def _vevent(binding: str, result: str) -> dict[str, object]:
    return {
        "event_type": "verification",
        "subject_id": binding,
        "subject_kind": "binding",
        "project_id": "p1",
        "ts_utc": "2026-06-07T10:03:00.000Z",
        "payload": {"binding": binding, "result": result},
    }


def _revise(shape: str, iteration: int, *, fb: int = 0, fd: int = 0, verdict: str = "converged") -> dict[str, object]:
    return {
        "event_type": "revise_round",
        "project_id": "p1",
        "iteration_index": iteration,
        "ts_utc": "2026-06-07T10:04:00.000Z",
        "payload": {"shape": shape, "round": 1, "findings_blocker": fb, "findings_drift": fd, "verdict": verdict},
    }


def test_build_binding_outcomes() -> None:
    outcomes = build_binding_outcomes(
        [
            _vevent("cf-pytest", "pass"),
            _vevent("cf-code-review", "fail"),
            {"event_type": "verification", "subject_kind": "audit_target", "subject_id": "x", "payload": {}},
            _ev("gate_fire", "g"),  # non-verification event ignored
        ]
    )
    by = {o.binding: o.passed for o in outcomes}
    assert by == {"cf-pytest": True, "cf-code-review": False}


def test_build_shape_usages_required_revision() -> None:
    usages = build_shape_usages(
        [
            _revise("component_build", 1, verdict="converged"),  # no revision
            _revise("skill_build", 2, fb=1, verdict="revise"),  # required revision
            {"event_type": "revise_round", "iteration_index": 3, "payload": {}},  # no shape → skipped
        ]
    )
    by = {(u.shape, u.required_reviewer_revision) for u in usages}
    assert ("component_build", False) in by
    assert ("skill_build", True) in by
    assert len(usages) == 2


def test_assemble_run_facts_all_three() -> None:
    events = [
        _ev("gate_fire", "g1", cls="gate_human"),
        _ev("gate_resolve", "g1", option="A"),
        _vevent("cf-pytest", "pass"),
        _revise("component_build", 1, fb=2, verdict="revise"),
    ]
    facts = assemble_run_facts(events)
    assert facts.gate_events and facts.binding_outcomes and facts.shape_usages
    assert facts.gate_events[0].resolved_option == "A"
    assert facts.binding_outcomes[0].passed is True
    assert facts.shape_usages[0].required_reviewer_revision is True


def test_run_facts_from_run_reads_all_three(tmp_path: object) -> None:
    import json as _json
    from pathlib import Path

    seed_dir = Path(str(tmp_path)) / "proj"
    logs = seed_dir / "state" / "logs"
    logs.mkdir(parents=True)
    events = [
        _ev("gate_fire", "g1", cls="gate_human"),
        _ev("gate_resolve", "g1", option="A"),
        _vevent("cf-pytest", "pass"),
        _revise("component_build", 1, fb=1, verdict="revise"),
    ]
    (logs / "events.jsonl").write_text("\n".join(_json.dumps(e) for e in events), encoding="utf-8")
    row = {"project_id": "p1", "seed_path": str(seed_dir / "seed.md")}
    facts = run_facts_from_run(row)
    assert len(facts.gate_events) == 1
    assert len(facts.binding_outcomes) == 1
    assert len(facts.shape_usages) == 1


def test_fr051_and_fr052_findings_fire() -> None:
    """End-to-end: an always-pass binding + an always-fail binding + a revision-prone shape across
    3 Runs yield over_verification, binding_defect, and session-shape findings."""
    def _facts(_row: object) -> RunFacts:
        return assemble_run_facts(
            [
                _vevent("cf-corpus-auditor", "pass"),  # never catches anything → over_verification
                _vevent("cf-flaky-binding", "fail"),  # always fails → binding_defect
                _revise("decision_q_block", 1, fb=1, verdict="revise"),  # always needs revision
            ]
        )

    rows = [_row(run_id="r1"), _row(run_id="r2"), _row(run_id="r3")]
    report = run_audit_pass(
        completed_run_records(rows, facts_for=_facts),
        config=AuditConfig(min_consistent_runs=3, shape_revision_fraction=0.5),
    )
    kinds = {f.kind for f in report.findings}
    assert FindingKind.VERIFICATION_BINDING in kinds
    assert FindingKind.SESSION_SHAPE in kinds
    binding_classes = {f.binding_class for f in report.findings if f.kind is FindingKind.VERIFICATION_BINDING}
    assert BindingFindingClass.OVER_VERIFICATION in binding_classes
    assert BindingFindingClass.BINDING_DEFECT in binding_classes


# --- Auto-feedback bridge: findings -> one-confirm operator escalations -------


def test_findings_to_escalations_surfaces_only_new_keys() -> None:
    from datetime import datetime, timezone

    findings = [
        AuditFinding(
            kind=FindingKind.ANSWERER_DSL_CANDIDATE,
            subject="g1",
            evidence="e",
            recommendation="add an Answerer rule pre-resolving 'g1' to 'A'",
            routes_to="operator + cf-spec-writer",
        ),
        AuditFinding(
            kind=FindingKind.SESSION_SHAPE,
            subject="migration_author",
            evidence="e",
            recommendation="tune the shape",
            routes_to="operator + cf-session-plan-reviewer",
        ),
    ]
    now = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)
    # Only g1 is NEW (the shape finding is already-known → not re-surfaced).
    escalations = findings_to_escalations(
        findings, new_keys={"answerer_dsl_candidate:g1"}, now=now
    )

    assert len(escalations) == 1
    esc = escalations[0]
    assert esc.gate_id == "learning:answerer_dsl_candidate:g1"
    assert esc.kind == "routine"  # not urgent
    assert esc.reversible is True
    assert esc.suggested_option == "add an Answerer rule pre-resolving 'g1' to 'A'"
    assert esc.confidence >= 0.7
    # The escalation is eligible for the FR-032 one-confirm accept path.
    assert auto_pick_eligible(esc) is True


def test_build_correction_attempts() -> None:
    events = [
        {
            "event_type": "correction_attempt",
            "event_uuid": "u1",
            "project_id": "p1",
            "iteration_index": 2,
            "subject_id": "OLB-07",
            "ts_utc": "2026-06-07T10:00:00.000Z",
            "payload": {"attempt": 2, "level": "L3", "item_id": "OLB-07"},
        },
        {"event_type": "correction_attempt", "project_id": "p1", "payload": {}},  # no uuid → skip
        {"event_type": "gate_fire", "event_uuid": "u2", "payload": {}},  # not a correction → skip
    ]
    attempts = build_correction_attempts(events)
    assert len(attempts) == 1
    a = attempts[0]
    assert a.event_uuid == "u1" and a.item_id == "OLB-07" and a.level == "L3" and a.attempt == 2


def test_derive_correction_findings_fires_on_recurrence() -> None:
    # Same item corrected across 3 distinct Runs → a chronic-defect finding (deepest L4).
    records = [
        CorrectionRecord(run_id="r1", project_slug="p", item_id="OLB-07", level="L2"),
        CorrectionRecord(run_id="r2", project_slug="p", item_id="OLB-07", level="L4"),
        CorrectionRecord(run_id="r3", project_slug="p", item_id="OLB-07", level="L3"),
        CorrectionRecord(run_id="r1", project_slug="p", item_id="OLB-99", level="L1"),  # 1 run only
    ]
    findings = derive_correction_findings(records, config=AuditConfig(min_consistent_runs=3))
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.CORRECTION_PATTERN
    assert findings[0].subject == "OLB-07"
    assert "L4" in findings[0].evidence  # deepest level reported


def test_findings_to_escalations_empty_when_no_new() -> None:
    from datetime import datetime, timezone

    findings = [
        AuditFinding(
            kind=FindingKind.ANSWERER_DSL_CANDIDATE,
            subject="g1",
            evidence="e",
            recommendation="r",
            routes_to="x",
        )
    ]
    assert findings_to_escalations(findings, new_keys=set(), now=datetime.now(timezone.utc)) == []


def test_read_events_jsonl_missing_file(tmp_path: object) -> None:
    from pathlib import Path

    assert read_events_jsonl(Path(str(tmp_path)) / "nope.jsonl") == []


def test_gate_events_from_run_scopes_to_window(tmp_path: object) -> None:
    import json as _json
    from pathlib import Path

    seed_dir = Path(str(tmp_path)) / "proj"
    logs = seed_dir / "state" / "logs"
    logs.mkdir(parents=True)
    lines = [
        # in-window gate
        _ev("gate_fire", "in", cls="gate_human") | {"ts_utc": "2026-06-07T10:05:00.000Z"},
        _ev("gate_resolve", "in", option="X") | {"ts_utc": "2026-06-07T10:06:00.000Z"},
        # out-of-window gate (a later, different Run sharing the append-only log)
        _ev("gate_fire", "later", cls="gate_human") | {"ts_utc": "2026-06-07T20:00:00.000Z"},
    ]
    (logs / "events.jsonl").write_text(
        "\n".join(_json.dumps(line) for line in lines), encoding="utf-8"
    )

    row = {
        "project_id": "p1",
        "seed_path": str(seed_dir / "seed.md"),
        "spawned_at": "2026-06-07T10:00:00+00:00",
        "terminated_at": "2026-06-07T10:10:00+00:00",
    }
    gates = {g.gate_id: g for g in gate_events_from_run(row)}
    assert "in" in gates and gates["in"].resolved_option == "X"
    assert "later" not in gates  # outside the Run's [spawned_at, terminated_at] window
