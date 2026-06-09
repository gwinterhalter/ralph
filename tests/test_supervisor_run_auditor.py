"""Component tests for the OLB-15 Run-Auditor (``supervisor/run_auditor.py``).

Covers the cross-run learning pass (Spec v1.3 §12) — one ``@pytest.mark.unit`` case
per FR-049..FR-053 plus two edges — entirely DB-free over in-memory
:class:`RunRecord` fixtures with an injected :class:`AuditConfig` (gate
``olb15-run-auditor-build-substrate`` = A). The Run-Auditor is a pure, read-only,
findings-only module, so every case is a direct call with no port, no database, no
``state\\`` tree read, no file I/O, and no wall-clock read: the findings are *surfaced*
(not adopted), and the no-mutation / no-substrate invariants are asserted mechanically.

The fixtures are crafted so each FR rule is isolated: the FR-050 case proves a
consistent resolution surfaces while a differing one does not; the FR-051 case holds
three bindings (uniform-pass, uniform-fail, mixed) to prove the classifier; the FR-052
case holds one shape at/above the fraction and one below it.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from supervisor import run_auditor
from supervisor.run_auditor import (
    ROUTE_ANSWERER_DSL,
    ROUTE_BINDINGS,
    ROUTE_SHAPES,
    AuditConfig,
    BindingFindingClass,
    BindingOutcome,
    FindingKind,
    GateEvent,
    RunRecord,
    ShapeUsage,
    derive_answerer_dsl_candidates,
    derive_binding_findings,
    derive_shape_findings,
    render_audit_report,
    run_audit_pass,
)

# A fixed injected config — the module reads neither a seed nor a wall-clock; both the
# Run threshold and the shape fraction are supplied here (gate substrate = A).
CONFIG = AuditConfig(min_consistent_runs=3, shape_revision_fraction=0.5)


def _consistent_escalation_runs() -> list[RunRecord]:
    """Three Runs whose ``gate.budget.softcap`` is escalated and resolved 'approve'."""
    return [
        RunRecord(
            run_id=f"run-{i}",
            project_slug="demo",
            status="complete" if i < 2 else "failed",
            gate_events=(
                GateEvent(
                    gate_id="gate.budget.softcap",
                    escalated_to_gate_human=True,
                    resolved_option="approve",
                ),
            ),
        )
        for i in range(3)
    ]


@pytest.mark.unit
def test_fr049_read_only_cross_run_pass_emits_findings_without_mutation() -> None:
    """FR-049: the pass emits findings over complete+failed Runs and mutates nothing."""
    runs = [
        RunRecord(
            run_id="run-0",
            project_slug="demo",
            status="complete",
            gate_events=(
                GateEvent("gate.budget.softcap", True, "approve"),
            ),
            binding_outcomes=(BindingOutcome("cf-pytest@component_build", True),),
            shape_usages=(ShapeUsage("component_build", True),),
        ),
        RunRecord(
            run_id="run-1",
            project_slug="demo",
            status="complete",
            gate_events=(
                GateEvent("gate.budget.softcap", True, "approve"),
            ),
            binding_outcomes=(BindingOutcome("cf-pytest@component_build", True),),
            shape_usages=(ShapeUsage("component_build", True),),
        ),
        RunRecord(
            run_id="run-2",
            project_slug="demo",
            status="failed",
            gate_events=(
                GateEvent("gate.budget.softcap", True, "approve"),
            ),
            binding_outcomes=(BindingOutcome("cf-pytest@component_build", True),),
            shape_usages=(ShapeUsage("component_build", False),),
        ),
    ]
    before = copy.deepcopy(runs)

    report = run_audit_pass(runs, config=CONFIG)

    assert report.runs_audited == 3
    assert report.min_consistent_runs == 3
    assert len(report.findings) >= 1  # at least the FR-050 + FR-051 patterns surface
    # The pass is read-only: every input record is byte-identical afterwards.
    assert runs == before


@pytest.mark.unit
def test_fr050_answerer_dsl_candidate_on_consistent_escalation() -> None:
    """FR-050: identical resolution across N Runs surfaces; a differing one does not."""
    findings = derive_answerer_dsl_candidates(
        _consistent_escalation_runs(), config=CONFIG
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind is FindingKind.ANSWERER_DSL_CANDIDATE
    assert finding.subject == "gate.budget.softcap"
    assert "approve" in finding.evidence
    assert finding.routes_to == ROUTE_ANSWERER_DSL

    # The SAME pattern resolved with DIFFERING options across the Runs -> no finding.
    differing = _consistent_escalation_runs()
    differing[2] = RunRecord(
        run_id="run-2",
        project_slug="demo",
        status="failed",
        gate_events=(
            GateEvent("gate.budget.softcap", True, "deny"),
        ),
    )
    assert derive_answerer_dsl_candidates(differing, config=CONFIG) == []


@pytest.mark.unit
def test_fr051_binding_findings_over_verification_and_defect() -> None:
    """FR-051: uniform-pass -> over_verification, uniform-fail -> defect, mixed -> none."""
    runs = [
        RunRecord(
            run_id=f"run-{i}",
            project_slug="demo",
            status="complete",
            binding_outcomes=(
                BindingOutcome("always-pass", True),
                BindingOutcome("always-fail", False),
                # 'mixed' fails only in the last Run -> a non-uniform record.
                BindingOutcome("mixed", i < 2),
            ),
        )
        for i in range(3)
    ]
    findings = derive_binding_findings(runs, config=CONFIG)
    by_subject = {f.subject: f for f in findings}

    assert set(by_subject) == {"always-pass", "always-fail"}  # 'mixed' surfaces nothing
    assert by_subject["always-pass"].binding_class is BindingFindingClass.OVER_VERIFICATION
    assert by_subject["always-fail"].binding_class is BindingFindingClass.BINDING_DEFECT
    assert by_subject["always-pass"].routes_to == ROUTE_BINDINGS


@pytest.mark.unit
def test_fr052_shape_finding_on_consistent_revision() -> None:
    """FR-052: a shape revised in >= the fraction of its uses surfaces; below does not."""
    # 'component_build' revised in 2 of 4 uses (0.50 >= 0.50 -> finding);
    # 'checkpoint' revised in 1 of 4 uses (0.25 < 0.50 -> no finding).
    revision_flags = {
        "component_build": [True, True, False, False],
        "checkpoint": [True, False, False, False],
    }
    runs = [
        RunRecord(
            run_id=f"run-{i}",
            project_slug="demo",
            status="complete",
            shape_usages=tuple(
                ShapeUsage(shape, flags[i]) for shape, flags in revision_flags.items()
            ),
        )
        for i in range(4)
    ]
    findings = derive_shape_findings(runs, config=CONFIG)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind is FindingKind.SESSION_SHAPE
    assert finding.subject == "component_build"
    assert finding.routes_to == ROUTE_SHAPES


@pytest.mark.unit
def test_fr053_findings_only_no_auto_adoption() -> None:
    """FR-053: the module surfaces findings + text and exposes no adopt/apply/write hook."""
    report = run_audit_pass(_consistent_escalation_runs(), config=CONFIG)
    rendered = render_audit_report(report)

    assert isinstance(rendered, str)
    assert "Run-Auditor report" in rendered
    # Every finding routes adoption to the operator + an authoring skill (surface-only).
    for finding in report.findings:
        assert finding.routes_to.startswith("operator + ")

    # The module exposes NO function that would adopt/apply/write a change.
    forbidden = ("adopt", "apply", "write", "mutate", "commit", "save", "persist")
    public_callables = [
        name
        for name in dir(run_auditor)
        if not name.startswith("_") and callable(getattr(run_auditor, name))
    ]
    for name in public_callables:
        assert not any(token in name.lower() for token in forbidden), name


@pytest.mark.unit
def test_run_auditor_no_substrate_access() -> None:
    """Edge: the module source touches no DB / port / file / wall-clock substrate."""
    source = Path(run_auditor.__file__).read_text(encoding="utf-8")
    assert "import supervisor.registry" not in source
    assert "from supervisor.registry" not in source
    assert "psycopg" not in source
    assert "open(" not in source
    assert ".execute(" not in source
    assert "datetime.now(" not in source
    # ``\bport.`` matches a real ``port.read_*`` call but not the word 'report.'.
    assert re.search(r"\bport\.", source) is None


@pytest.mark.unit
def test_audit_pass_empty_run_set_yields_empty_report() -> None:
    """Edge: zero Runs -> an empty-findings report and a 'No findings.' render, no raise."""
    report = run_audit_pass([], config=CONFIG)
    assert report.runs_audited == 0
    assert report.findings == ()
    assert "No findings." in render_audit_report(report)
