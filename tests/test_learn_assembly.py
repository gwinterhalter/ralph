"""Item 2 — live assembly for the §4.4 step-6 Learn pass (supervisor.learn_assembly)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from supervisor.learn_assembly import (
    LearningRecord,
    build_gate_events,
    completed_run_records,
    gate_events_from_run,
    learning_records,
    read_events_jsonl,
    render_learning_corpus,
)
from supervisor.run_auditor import AuditConfig, FindingKind, GateEvent, RunRecord, run_audit_pass

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


def test_completed_run_records_threads_gate_events() -> None:
    def _facts(_row: object) -> tuple[GateEvent, ...]:
        return (GateEvent(gate_id="g1", escalated_to_gate_human=True, resolved_option="A"),)

    records = completed_run_records([_row(run_id="a")], gate_events_for=_facts)
    assert records[0].gate_events == (
        GateEvent(gate_id="g1", escalated_to_gate_human=True, resolved_option="A"),
    )
    # Default (no callable) leaves them empty (back-compat).
    assert completed_run_records([_row()])[0].gate_events == ()


def test_fr050_finding_fires_over_assembled_gate_events() -> None:
    """End-to-end: a gate escalated + resolved with the SAME option across 3 Runs (the assembled
    facts) yields an Answerer-DSL candidate — Layer-2 learnings now light up."""
    def _facts(_row: object) -> tuple[GateEvent, ...]:
        return (GateEvent(gate_id="recurring-gate", escalated_to_gate_human=True, resolved_option="B"),)

    rows = [_row(run_id="r1"), _row(run_id="r2"), _row(run_id="r3")]
    records = completed_run_records(rows, gate_events_for=_facts)
    report = run_audit_pass(records, config=AuditConfig(min_consistent_runs=3))

    dsl = [f for f in report.findings if f.kind is FindingKind.ANSWERER_DSL_CANDIDATE]
    assert len(dsl) == 1
    assert dsl[0].subject == "recurring-gate"
    assert "B" in dsl[0].recommendation


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
