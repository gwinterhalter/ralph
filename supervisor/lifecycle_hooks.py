"""Teardown lifecycle hooks for the Outer Loop Supervisor (OLB-13 / §14).

A pure, DB-free plan layer (Spec v1.3 §14) that computes — but never executes — the
teardown of a Project that has reached a terminal lifecycle state. Given the
Project's identity, the observed Run terminal outcome, and the current concurrency
picture, it derives the FR-055 reconcile-and-slot-release plan (composing the closed
OLB-08 ``supervisor/run_lifecycle.py`` read-only), asserts the FR-056 durable-artefact
invariant (a teardown touches only the Run row + lifecycle state + concurrency slot —
never a design zone, work-registry closure, or ``state\\`` path), and models the
FR-057 re-admission entry (a torn-down Project re-enters only as a fresh
``candidate``, never by resuming the torn-down Run). It performs no live DB write,
deletes nothing, reads no wall-clock, and imports nothing from
``supervisor.registry``: it operates only on supplied inputs and the read-only-composed
``run_lifecycle`` / ``transitions`` shapes. Resolved per gates
``olb13-repair-teardown-build-substrate`` (option A — DB-free),
``olb13-repair-teardown-build-scope`` (option A — pure layer), and
``olb13-teardown-run-lifecycle-composition`` (option A — compose ``run_lifecycle``
read-only, do not edit it).

Spec mapping (§14.2):

* FR-055 Teardown reconcile + slot release — :func:`plan_teardown` fires for a
  Project reaching ``complete`` OR ``failed``; it derives the final Run-row reconcile
  (delegating the complete-path shape to the
  :data:`supervisor.run_lifecycle.RUN_STATUS_COMPLETE` /
  :data:`~supervisor.run_lifecycle.LIFECYCLE_COMPLETE` constants) and the matching
  final lifecycle state, and signals the released concurrency slot (a freed slot the
  scheduler may hand to a waiting ``admitted`` Project).
* FR-056 Durable-artefact preservation — :func:`assert_durable_artifacts_preserved`
  verifies a teardown plan references ONLY the Run row + lifecycle state + concurrency
  slot, never a design zone / work-registry / ``state\\`` path (no delete, no rewrite).
* FR-057 Re-admission as a fresh candidate — :func:`re_admission_entry` models that a
  torn-down ``complete`` / ``failed`` Project re-enters only as a fresh ``candidate``
  (FR-008), never by resuming the torn-down Run; the legality is verified read-only
  against ``supervisor/transitions.py``.

§14 boundary: this module COMPUTES the plan; the caller (OLB-14/OLB-16) executes it
through the existing OLB-02/08a ``RegistryPort`` (``reconcile_run`` +
``set_lifecycle_state``). The §14 seed-validation admission hook (FR-054) is out of
OLB-13 scope — already delivered by OLB-07 ``supervisor/seed_validation.py``.

Note on the ``failed`` terminal: ``run_lifecycle.py`` (OLB-08) ships only the
``complete``-path constants (it reconciles a clean INITIATIVE_COMPLETE drain). The
``failed`` terminal — legal per ``transitions`` (``running -> failed``,
``failed -> candidate``) — is modelled here with local constants rather than by
editing the closed seam (gate ``olb13-teardown-run-lifecycle-composition`` option A).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from supervisor import run_lifecycle, transitions
from supervisor.run_lifecycle import RunTerminal

# --- Constants ---------------------------------------------------------------

#: The terminal Run status / Project lifecycle state for a Run that did NOT reach a
#: clean INITIATIVE_COMPLETE drain (Spec v1.3 §5.3 — ``running -> failed`` is legal).
#: ``run_lifecycle.py`` ships only the ``complete``-path constants; the ``failed``
#: terminal is modelled here so the closed seam stays unedited.
RUN_STATUS_FAILED = "failed"
LIFECYCLE_FAILED = "failed"

#: The lifecycle state a torn-down Project re-enters through (Spec v1.3 §5.3 FR-008 /
#: §14 FR-057). Re-admission is always a fresh ``candidate``; a direct return to the
#: torn-down Run is illegal.
RE_ADMISSION_STATE = "candidate"

#: The resource kinds a teardown plan is permitted to touch (Spec v1.3 §14 FR-056):
#: the Run Registry row, the Project lifecycle state, and the concurrency slot. A plan
#: naming anything else (a design zone, a work-registry closure, a ``state\\`` path) is
#: a durable-artefact violation.
RESOURCE_RUN_ROW = "run_row"
RESOURCE_LIFECYCLE_STATE = "lifecycle_state"
RESOURCE_CONCURRENCY_SLOT = "concurrency_slot"
ALLOWED_TEARDOWN_RESOURCES = frozenset(
    {RESOURCE_RUN_ROW, RESOURCE_LIFECYCLE_STATE, RESOURCE_CONCURRENCY_SLOT}
)

#: Path fragments that must NEVER appear in a teardown plan's touched resources
#: (Spec v1.3 §14 FR-056 — durable artefacts are preserved, never deleted/rewritten).
#: Case-folded substring markers for the design zone, the work-registry, and the
#: ``state\\`` tree.
_FORBIDDEN_PATH_MARKERS = (
    "\\design\\",
    "/design/",
    "\\state\\",
    "/state/",
    "register",
    "project_docs",
)


class DurableArtifactViolation(AssertionError):
    """Raised when a teardown plan would touch a durable artefact it must preserve
    (Spec v1.3 §14 FR-056)."""


# --- Value objects -----------------------------------------------------------


@dataclass(frozen=True)
class TeardownProject:
    """The Project a teardown fires for (Spec v1.3 §14.2).

    Carries the ``project_id`` and the ``lifecycle_state`` it currently holds (a
    ``running`` Project reaching its terminal). The field names mirror the Registry
    ``projects`` columns the OLB-02 ``RegistryPort`` reads. Frozen — the hook derives
    a plan, never mutates the Project.
    """

    project_id: str
    lifecycle_state: str


@dataclass(frozen=True)
class TeardownPlan:
    """The computed FR-055 teardown plan for a terminal Project (Spec v1.3 §14.2).

    Carries the Run-row reconcile target (``run_status`` ``complete`` / ``failed``,
    ``terminated_at``, ``terminal_cost_usd`` as an exact ``Decimal`` per NFR-007), the
    matching ``final_lifecycle_state``, and the slot-release signal (``releases_slot``
    plus ``freed_slot_available`` — whether the released slot leaves headroom a waiting
    ``admitted`` Project may take). ``touched_resources`` enumerates exactly the
    resource kinds the plan acts on (FR-056 — only the Run row + lifecycle state +
    concurrency slot). This module COMPUTES the plan; the caller (OLB-14/OLB-16)
    executes it via the existing ``RegistryPort``.
    """

    project_id: str
    run_status: str
    terminated_at: str
    terminal_cost_usd: Decimal
    final_lifecycle_state: str
    releases_slot: bool
    freed_slot_available: bool
    touched_resources: tuple[str, ...] = field(
        default=(
            RESOURCE_RUN_ROW,
            RESOURCE_LIFECYCLE_STATE,
            RESOURCE_CONCURRENCY_SLOT,
        )
    )


@dataclass(frozen=True)
class ReAdmissionEntry:
    """The FR-057 re-admission entry for a torn-down Project (Spec v1.3 §14.2).

    A torn-down ``complete`` / ``failed`` Project re-enters ONLY as a fresh
    ``candidate`` (``lifecycle_state`` :data:`RE_ADMISSION_STATE`), and never by
    resuming the torn-down Run (``resumes_torn_down_run`` is always ``False``). The
    legality of the ``terminal -> candidate`` edge is verified read-only against
    ``supervisor/transitions.py`` when the entry is built.
    """

    project_id: str
    lifecycle_state: str = RE_ADMISSION_STATE
    resumes_torn_down_run: bool = False


# --- FR-055: teardown reconcile + slot release -------------------------------


def plan_teardown(
    project: TeardownProject,
    run_terminal: RunTerminal,
    *,
    running_count: int,
    concurrency_ceiling: int,
) -> TeardownPlan:
    """Compute the FR-055 teardown plan for a terminal Project (Spec v1.3 §14.2).

    Fires for a Project reaching a terminal state. The Run terminal outcome decides
    the path: a completed :class:`~supervisor.run_lifecycle.RunTerminal` (a clean
    INITIATIVE_COMPLETE drain) reconciles the Run row to
    :data:`~supervisor.run_lifecycle.RUN_STATUS_COMPLETE` and the Project to
    :data:`~supervisor.run_lifecycle.LIFECYCLE_COMPLETE` (composing the closed OLB-08
    constants read-only); a non-completed terminal reconciles to the local
    :data:`RUN_STATUS_FAILED` / :data:`LIFECYCLE_FAILED` (``running -> failed`` is a
    legal §5.3 edge). Either way it carries the ``terminated_at`` + exact-``Decimal``
    ``terminal_cost_usd`` from the terminal, and signals slot release: the Project's
    concurrency slot is freed, and ``freed_slot_available`` is true iff the release
    leaves headroom (``running_count - 1 < concurrency_ceiling``) a waiting
    ``admitted`` Project may take. COMPUTES only — no live ``port.*`` call, no DB
    write, no file delete.
    """
    if run_terminal.completed:
        run_status = run_lifecycle.RUN_STATUS_COMPLETE
        final_lifecycle_state = run_lifecycle.LIFECYCLE_COMPLETE
    else:
        run_status = RUN_STATUS_FAILED
        final_lifecycle_state = LIFECYCLE_FAILED

    # The terminal Run-row reconcile is only legal from ``running``; the slot it holds
    # is the one released back to the Concurrency Ceiling (§14 FR-055).
    freed_headroom = concurrency_ceiling - (running_count - 1)
    return TeardownPlan(
        project_id=project.project_id,
        run_status=run_status,
        terminated_at=run_terminal.terminated_at,
        terminal_cost_usd=run_terminal.terminal_cost_usd,
        final_lifecycle_state=final_lifecycle_state,
        releases_slot=True,
        freed_slot_available=freed_headroom >= 1,
    )


# --- FR-056: durable-artefact preservation -----------------------------------


def assert_durable_artifacts_preserved(plan: TeardownPlan) -> None:
    """Assert ``plan`` preserves every durable artefact (Spec v1.3 §14 FR-056).

    A teardown reconciles the Run row + lifecycle state and releases the concurrency
    slot — and touches NOTHING else. This verifies every entry of
    ``plan.touched_resources`` is one of :data:`ALLOWED_TEARDOWN_RESOURCES` and that no
    touched resource names a design zone, work-registry, or ``state\\`` path (the
    :data:`_FORBIDDEN_PATH_MARKERS`). Raises :class:`DurableArtifactViolation` on any
    out-of-set or path-bearing resource — so a plan that would delete or rewrite a
    durable artefact fails fast rather than executing.
    """
    for resource in plan.touched_resources:
        if resource not in ALLOWED_TEARDOWN_RESOURCES:
            raise DurableArtifactViolation(
                f"teardown plan for {plan.project_id!r} touches out-of-set resource "
                f"{resource!r}; a teardown may touch only {sorted(ALLOWED_TEARDOWN_RESOURCES)} "
                "(FR-056 — durable artefacts are preserved)."
            )
        folded = resource.lower()
        for marker in _FORBIDDEN_PATH_MARKERS:
            if marker in folded:
                raise DurableArtifactViolation(
                    f"teardown plan for {plan.project_id!r} names a durable-artefact "
                    f"path in resource {resource!r} (marker {marker!r}); a teardown "
                    "never deletes or rewrites a design zone / work-registry / "
                    "state\\ artefact (FR-056)."
                )


# --- FR-057: re-admission as a fresh candidate -------------------------------


def re_admission_entry(project: TeardownProject) -> ReAdmissionEntry:
    """Model the FR-057 re-admission entry for a torn-down Project (Spec v1.3 §14.2).

    A torn-down ``complete`` / ``failed`` Project re-enters ONLY as a fresh
    ``candidate`` (:data:`RE_ADMISSION_STATE`), never by resuming the torn-down Run.
    Verifies the ``terminal -> candidate`` edge is legal read-only against
    ``supervisor/transitions.py`` (so re-admission keys on the real lifecycle model,
    not an assumed one) and raises :class:`ValueError` if called for a Project not in a
    terminal (``complete`` / ``failed``) state. Returns a :class:`ReAdmissionEntry`;
    the live ``set_lifecycle_state`` transition is the caller's (OLB-14/OLB-16).
    """
    if not transitions.is_legal_transition(
        project.lifecycle_state, RE_ADMISSION_STATE
    ):
        raise ValueError(
            f"re-admission of {project.project_id!r} from "
            f"{project.lifecycle_state!r} -> {RE_ADMISSION_STATE!r} is not a legal "
            "lifecycle transition; FR-057 re-admission applies only to a torn-down "
            "terminal (complete / failed) Project."
        )
    return ReAdmissionEntry(project_id=project.project_id)


__all__ = [
    "RUN_STATUS_FAILED",
    "LIFECYCLE_FAILED",
    "RE_ADMISSION_STATE",
    "RESOURCE_RUN_ROW",
    "RESOURCE_LIFECYCLE_STATE",
    "RESOURCE_CONCURRENCY_SLOT",
    "ALLOWED_TEARDOWN_RESOURCES",
    "DurableArtifactViolation",
    "TeardownProject",
    "TeardownPlan",
    "ReAdmissionEntry",
    "plan_teardown",
    "assert_durable_artifacts_preserved",
    "re_admission_entry",
]
