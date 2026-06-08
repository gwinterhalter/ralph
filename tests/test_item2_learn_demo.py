"""Item 2 — end-to-end Learn demonstration over 3 mock ABS-fleet projects.

Reads three mock project event logs under ``tests/fixtures/learn_demo/`` (abs_phase0/1/2 — the
ABS Phase 0->1->2 chain) through the REAL ``run_facts_from_run`` file path, runs the full
Run-Auditor, and asserts all three Layer-2 learning kinds fire — proving FR-050 (gate /
Answerer-DSL), FR-051 (verification-binding), and FR-052 (session-shape) end-to-end against
on-disk mock projects (not in-memory fakes).

Each mock project's events.jsonl exhibits, across the 3 runs:
  * a recurring human gate ``abs-phase-boundary-confirm`` always resolved ``proceed``
    -> FR-050 Answerer-DSL candidate;
  * binding ``cf-corpus-auditor`` always passing -> FR-051 over_verification;
  * binding ``cf-fr-stages-auditor`` always failing -> FR-051 binding_defect;
  * shape ``spec_review_loop`` requiring reviewer revision in 2 of 3 uses -> FR-052.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from supervisor.learn_assembly import completed_run_records, run_facts_from_run
from supervisor.run_auditor import (
    AuditConfig,
    BindingFindingClass,
    FindingKind,
    render_audit_report,
    run_audit_pass,
)

pytestmark = pytest.mark.unit

_DEMO_ROOT = Path(__file__).resolve().parent / "fixtures" / "learn_demo"
_PROJECTS = ("abs_phase0", "abs_phase1", "abs_phase2")


def _rows() -> list[dict[str, object]]:
    """One completed-Run row per mock project (seed_path -> the fixture project dir)."""
    return [
        {
            "run_id": f"{p}-run",
            "project_id": p,
            "status": "complete",
            "seed_path": str(_DEMO_ROOT / p / "seed.md"),
        }
        for p in _PROJECTS
    ]


def test_item2_demo_all_three_finding_kinds_fire() -> None:
    records = completed_run_records(_rows(), facts_for=run_facts_from_run)
    assert len(records) == 3  # all three mock runs read from disk

    report = run_audit_pass(
        records, config=AuditConfig(min_consistent_runs=3, shape_revision_fraction=0.5)
    )
    kinds = {f.kind for f in report.findings}
    assert FindingKind.ANSWERER_DSL_CANDIDATE in kinds  # FR-050
    assert FindingKind.VERIFICATION_BINDING in kinds  # FR-051
    assert FindingKind.SESSION_SHAPE in kinds  # FR-052

    binding_classes = {
        f.binding_class for f in report.findings if f.kind is FindingKind.VERIFICATION_BINDING
    }
    assert BindingFindingClass.OVER_VERIFICATION in binding_classes  # cf-corpus-auditor
    assert BindingFindingClass.BINDING_DEFECT in binding_classes  # cf-fr-stages-auditor

    # The rendered report is the operator-facing artifact (logs/run_auditor_report.md in production).
    rendered = render_audit_report(report)
    assert "abs-phase-boundary-confirm" in rendered
    assert "spec_review_loop" in rendered


def test_item2_demo_render_is_human_readable() -> None:
    report = run_audit_pass(
        completed_run_records(_rows(), facts_for=run_facts_from_run),
        config=AuditConfig(min_consistent_runs=3, shape_revision_fraction=0.5),
    )
    rendered = render_audit_report(report)
    assert rendered.startswith("# Run-Auditor report")
    assert "routes_to (adoption is operator-owned)" in rendered  # the adoption hook is surfaced
