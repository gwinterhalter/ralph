"""Component tests for the OLB-13 teardown hooks (``supervisor/lifecycle_hooks.py``).

Covers the OLB-13 §14 teardown predicate — one ``@pytest.mark.unit`` case per
FR-055..FR-057 plus a composition edge — entirely DB-free over in-memory
:class:`~supervisor.run_lifecycle.RunTerminal` + :class:`TeardownProject` fixtures
(gate ``olb13-repair-teardown-build-substrate`` = A). The hook is a pure plan layer,
so every case is a direct call with no port, no database, no file I/O, no wall-clock
read, and no file delete: the plan is *computed* and asserted, never executed. The
complete-path reconcile shape is verified to compose the closed OLB-08
``run_lifecycle`` constants read-only (``run_lifecycle.py`` unedited).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from supervisor import run_lifecycle
from supervisor.lifecycle_hooks import (
    ALLOWED_TEARDOWN_RESOURCES,
    LIFECYCLE_FAILED,
    RE_ADMISSION_STATE,
    RESOURCE_CONCURRENCY_SLOT,
    RUN_STATUS_FAILED,
    DurableArtifactViolation,
    ReAdmissionEntry,
    TeardownPlan,
    TeardownProject,
    assert_durable_artifacts_preserved,
    plan_teardown,
    re_admission_entry,
)
from supervisor.run_lifecycle import RunTerminal

# A fixed concurrency picture — the Concurrency Ceiling is 2 (Spec §3 / seed §2),
# supplied here (the hook reads no seed). One Run is in flight (the one tearing down).
CEILING = 2
RUNNING_COUNT = 1


def _complete_terminal(cost: str = "1.2345") -> RunTerminal:
    """A clean INITIATIVE_COMPLETE drain terminal (the FR-055 complete path)."""
    return RunTerminal(
        completed=True,
        exit_code=0,
        terminated_at="2026-06-05T12:00:00+00:00",
        terminal_cost_usd=Decimal(cost),
    )


def _failed_terminal(cost: str = "0.5000") -> RunTerminal:
    """A non-completed terminal (exited without INITIATIVE_COMPLETE) — the FR-055
    failed path."""
    return RunTerminal(
        completed=False,
        exit_code=1,
        terminated_at="2026-06-05T12:30:00+00:00",
        terminal_cost_usd=Decimal(cost),
        detail="orchestrator exited 1 without INITIATIVE_COMPLETE",
    )


@pytest.mark.unit
def test_fr055_teardown_reconciles_run_and_releases_slot() -> None:
    """FR-055: a Project reaching its terminal reconciles the Run row + Project
    lifecycle state and signals a freed slot a waiting ``admitted`` Project may take —
    on the complete path to ``complete`` and on the failed path to ``failed``."""
    project = TeardownProject(project_id="proj-a", lifecycle_state="running")

    complete = plan_teardown(
        project,
        _complete_terminal(),
        running_count=RUNNING_COUNT,
        concurrency_ceiling=CEILING,
    )
    assert complete.run_status == run_lifecycle.RUN_STATUS_COMPLETE
    assert complete.final_lifecycle_state == run_lifecycle.LIFECYCLE_COMPLETE
    assert complete.terminal_cost_usd == Decimal("1.2345")
    assert complete.terminated_at == "2026-06-05T12:00:00+00:00"
    assert complete.releases_slot is True
    # Releasing the one running slot leaves headroom under the ceiling of 2.
    assert complete.freed_slot_available is True

    failed = plan_teardown(
        project,
        _failed_terminal(),
        running_count=RUNNING_COUNT,
        concurrency_ceiling=CEILING,
    )
    assert failed.run_status == RUN_STATUS_FAILED
    assert failed.final_lifecycle_state == LIFECYCLE_FAILED
    assert failed.terminal_cost_usd == Decimal("0.5000")
    assert failed.releases_slot is True


@pytest.mark.unit
def test_fr056_durable_artifacts_preserved() -> None:
    """FR-056: a normal teardown plan touches only the Run row + lifecycle state +
    concurrency slot and passes the durable-artefact assertion; a plan that names a
    design-zone / work-registry / ``state\\`` path — or any out-of-set resource —
    fails fast."""
    plan = plan_teardown(
        TeardownProject(project_id="proj-a", lifecycle_state="running"),
        _complete_terminal(),
        running_count=RUNNING_COUNT,
        concurrency_ceiling=CEILING,
    )
    assert set(plan.touched_resources) <= ALLOWED_TEARDOWN_RESOURCES
    # A normal plan preserves every durable artefact (raises nothing).
    assert_durable_artifacts_preserved(plan)  # no DurableArtifactViolation

    # A plan that would touch a state\ path fails fast.
    tampered_state = TeardownPlan(
        project_id="proj-a",
        run_status=run_lifecycle.RUN_STATUS_COMPLETE,
        terminated_at="2026-06-05T12:00:00+00:00",
        terminal_cost_usd=Decimal("1.2345"),
        final_lifecycle_state=run_lifecycle.LIFECYCLE_COMPLETE,
        releases_slot=True,
        freed_slot_available=True,
        touched_resources=(
            RESOURCE_CONCURRENCY_SLOT,
            r"K:\Project_Docs\Sub_Projects\ol-build\state\seed.md",
        ),
    )
    with pytest.raises(DurableArtifactViolation):
        assert_durable_artifacts_preserved(tampered_state)

    # A plan naming an out-of-set resource (the work registry) fails fast.
    tampered_registry = TeardownPlan(
        project_id="proj-a",
        run_status=run_lifecycle.RUN_STATUS_COMPLETE,
        terminated_at="2026-06-05T12:00:00+00:00",
        terminal_cost_usd=Decimal("1.2345"),
        final_lifecycle_state=run_lifecycle.LIFECYCLE_COMPLETE,
        releases_slot=True,
        freed_slot_available=True,
        touched_resources=(RESOURCE_CONCURRENCY_SLOT, "work_registry_closure"),
    )
    with pytest.raises(DurableArtifactViolation):
        assert_durable_artifacts_preserved(tampered_registry)


@pytest.mark.unit
def test_fr057_re_admission_is_fresh_candidate() -> None:
    """FR-057: a torn-down ``complete`` (or ``failed``) Project re-enters only as a
    fresh ``candidate`` — never by resuming the torn-down Run — and re-admission from a
    non-terminal state is rejected as an illegal transition."""
    for terminal_state in ("complete", "failed"):
        entry = re_admission_entry(
            TeardownProject(project_id="proj-a", lifecycle_state=terminal_state)
        )
        assert isinstance(entry, ReAdmissionEntry)
        assert entry.lifecycle_state == RE_ADMISSION_STATE == "candidate"
        assert entry.resumes_torn_down_run is False

    # A non-terminal (running) Project cannot re-admit: running -> candidate is illegal.
    with pytest.raises(ValueError):
        re_admission_entry(
            TeardownProject(project_id="proj-a", lifecycle_state="running")
        )


@pytest.mark.unit
def test_teardown_composes_run_lifecycle_read_only() -> None:
    """Edge: the complete-path plan's reconcile shape matches what the closed OLB-08
    ``run_lifecycle.reconcile_run_complete`` would persist — Run row to
    ``RUN_STATUS_COMPLETE`` carrying the terminal's ``terminated_at`` +
    ``terminal_cost_usd``, Project to ``LIFECYCLE_COMPLETE`` — composing the constants
    read-only rather than duplicating the reconcile."""
    terminal = _complete_terminal(cost="3.0000")
    plan = plan_teardown(
        TeardownProject(project_id="proj-a", lifecycle_state="running"),
        terminal,
        running_count=RUNNING_COUNT,
        concurrency_ceiling=CEILING,
    )
    # The plan keys on the closed seam's own constants (composition, not assumed names).
    assert plan.run_status == run_lifecycle.RUN_STATUS_COMPLETE == "complete"
    assert plan.final_lifecycle_state == run_lifecycle.LIFECYCLE_COMPLETE == "complete"
    # And carries the terminal's reconcile fields verbatim (the FR-011/FR-014 shape
    # reconcile_run_complete hands to RegistryPort.reconcile_run).
    assert plan.terminated_at == terminal.terminated_at
    assert plan.terminal_cost_usd == terminal.terminal_cost_usd
