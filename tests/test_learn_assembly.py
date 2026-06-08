"""Item 2 — live assembly for the §4.4 step-6 Learn pass (supervisor.learn_assembly)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from supervisor.learn_assembly import (
    LearningRecord,
    completed_run_records,
    learning_records,
    render_learning_corpus,
)
from supervisor.run_auditor import RunRecord

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
