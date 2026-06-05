"""Project Lifecycle State transition legality for the Outer Loop Supervisor (OLB-02).

Pure, DB-free encoding of the Spec v1.3 §5.3 lifecycle-state model: the legal
Project Lifecycle State transition set (FR-008). The concrete Registry layer
(``supervisor/registry.py``) enforces this at the ``set_lifecycle_state`` write
boundary; nothing in this module touches a database.

Spec v1.3 §5.2 FR-001 fixes the eight lifecycle states; §5.3 fixes the legal
edges between them. An illegal transition (e.g. ``complete`` -> ``running``
without re-admission — the FR-008 acceptance criterion) is rejected here.

This is the Project Lifecycle State machine only. It is distinct from the legacy
``projects.status`` column and from the ``ralph_runs.status`` run-status enum
(§5.4), which the Registry layer validates separately.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

# The eight Project Lifecycle States (Spec v1.3 §5.2 FR-001).
LIFECYCLE_STATES: frozenset[str] = frozenset(
    {
        "candidate",
        "admitted",
        "running",
        "paused_gate",
        "paused_budget",
        "paused_safety",
        "complete",
        "failed",
    }
)

# The legal transition set (Spec v1.3 §5.3, FR-008) as an immutable
# {from_state: {to_states}} adjacency map. Every edge below is exactly one bullet
# of §5.3; no edge exists here that §5.3 does not list.
LEGAL_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        # candidate -> admitted: Admission Gate passed (§6).
        "candidate": frozenset({"admitted"}),
        # admitted -> running: Supervisor spawns the orchestrator Run (§6.3).
        "admitted": frozenset({"running"}),
        # running -> the three pause states, or complete, or failed (§5.3).
        "running": frozenset(
            {"paused_gate", "paused_budget", "paused_safety", "complete", "failed"}
        ),
        # paused_* -> running: the blocking condition cleared and the Run resumed.
        "paused_gate": frozenset({"running"}),
        "paused_budget": frozenset({"running"}),
        "paused_safety": frozenset({"running"}),
        # complete / failed -> candidate: operator re-opens with a fresh or revised
        # seed (re-admission required; a direct -> running is illegal per FR-008).
        "complete": frozenset({"candidate"}),
        "failed": frozenset({"candidate"}),
    }
)


class IllegalTransitionError(ValueError):
    """Raised when an illegal Project Lifecycle State transition is attempted
    (Spec v1.3 §5.3 FR-008)."""


def legal_targets(src: str) -> frozenset[str]:
    """Return the legal destination states reachable from ``src`` in one step.

    Returns an empty frozenset for a state with no outgoing legal edge or for an
    unknown state (one outside the §5.2 FR-001 enum).
    """
    return LEGAL_TRANSITIONS.get(src, frozenset())


def is_legal_transition(src: str, dst: str) -> bool:
    """Return ``True`` iff ``src -> dst`` is a legal §5.3 lifecycle transition.

    Unknown states (outside the §5.2 FR-001 enum) are never part of a legal
    transition, so this returns ``False`` for them rather than raising.
    """
    return dst in legal_targets(src)


def assert_legal_transition(src: str, dst: str) -> None:
    """Raise :class:`IllegalTransitionError` unless ``src -> dst`` is legal.

    The Registry layer calls this BEFORE persisting a lifecycle-state UPDATE, so
    an illegal transition never reaches the database (FR-008 enforced at the
    write boundary).
    """
    if not is_legal_transition(src, dst):
        raise IllegalTransitionError(
            f"illegal lifecycle transition {src!r} -> {dst!r} "
            f"(Spec v1.3 §5.3 FR-008); legal targets from {src!r}: "
            f"{sorted(legal_targets(src))}"
        )
