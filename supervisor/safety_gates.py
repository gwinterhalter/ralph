"""Safety-Gates — the hard floor for the Outer Loop Supervisor (OLB-06).

A substrate-light enforcement/decision layer encoding the Spec v1.3 §9 invariants
that hold *no matter what any scheduler, policy, or orchestrator does* (§9.1).
The gate is the hard floor, not advisory: a trip pauses the affected Project into
``paused_safety`` and raises a top-tier escalation (§8 FR-029) — it never kills a
Run (§9.2 FR-038).

Resolved per gate ``olb06-safety-gate-build-scope`` (option A — the recommended
default): OLB-06 ships the pure enforcement/decision primitives that the future
Dispatch paths CONSULT. The live machinery that *acts on* these decisions is
forward-referenced to the components that own it:

* FR-034 read-only-corpus invariant — :func:`check_dispatch_allowed` refuses any
  Dispatch whose Blast-Radius Scope omits the read-only corpus path, recording
  reason :data:`READ_ONLY_INVARIANT_VIOLATION`.
* FR-035 blast-radius enforcement — :func:`provision_blast_radius` returns exactly
  the recorded scope and no broader; :func:`scopes_disjoint` proves one Project's
  scope cannot reach another's substrate. The live MCP-root provisioning AT spawn
  is OLB-07 admit-and-spawn.
* FR-036 global Kill-Switch — :class:`KillSwitch` is the engage/disengage state
  primitive :func:`check_dispatch_allowed` honours (engaged => all Dispatch
  refused, overriding scheduler/admission state). The live signalling of running
  Runs to a safe stop and the end-to-end drill are OLB-09 / OLB-16 (C5).
* FR-037 concurrency-ceiling enforcement — :func:`check_dispatch_allowed` refuses
  any spawn that would exceed the ceiling as a HARD bound, independent of (and
  distinct from) OLB-07's FR-019 admission precondition.
* FR-038 trip-to-paused_safety — :func:`trip_to_paused_safety` moves a tripped
  Project to ``paused_safety`` via the OLB-02 ``RegistryPort.set_lifecycle_state``
  write seam and returns a top-tier :class:`SafetyEscalation`, never a kill. The
  §8 FR-029 surfacing/routing of that record is OLB-10.

§9.3 precedence: the gate refuses a Dispatch that would violate an invariant
regardless of which component (scheduler §7, admission §6, repair §11) requested
it. This is enforced structurally — the refusal branch conditions in
:func:`check_dispatch_allowed` reference only the invariant inputs (scope,
running-count, ceiling, kill-switch); the optional ``requested_by`` label is
carried into the human-readable refusal detail ONLY and never into a condition.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from supervisor.ports import RegistryPort

# --- Constants ---------------------------------------------------------------

# The lifecycle state a tripped Project is paused into (Spec v1.3 §5.2 FR-001;
# the ``running -> paused_safety`` edge is legal per §5.3, enforced by OLB-02's
# transitions table). The gate writes ONLY this state — never a terminal
# ``complete`` / ``failed`` (FR-038: pause, do not kill).
PAUSED_SAFETY = "paused_safety"

# The read-only corpus path the §9.2 FR-034 invariant requires every Blast-Radius
# Scope to list as read-only. Spec wording: "does not list ``Project_Docs_Current\\``
# (and the Project's own design zone) as read-only". A Project may additionally
# declare its own design zone on the scope (:attr:`BlastRadiusScope.design_zone`),
# which the same invariant then also requires to be read-only.
READ_ONLY_CORPUS_PATH = "Project_Docs_Current\\"

# Global Concurrency Ceiling (Spec v1.3 §3 / seed §2). Injected as a parameter so
# the gate sets no ceiling of its own — the authoritative owner is the OLB-09
# Cross-Project Scheduler (§8 forward-reference). OLB-06 enforces the hard bound.
DEFAULT_CONCURRENCY_CEILING = 2

# Top-tier escalation tier (Spec v1.3 §8 FR-029 — bypasses batching + quiet hours).
ESCALATION_TIER_TOP = "top_tier"

# --- Canonical refusal reason codes (§9.2) -----------------------------------
# One canonical refusal shape carries one of these structured reason codes; no
# ad-hoc refusal strings are produced anywhere in this module.

#: FR-034 — the Blast-Radius Scope omits the required read-only corpus path.
READ_ONLY_INVARIANT_VIOLATION = "read_only_invariant_violation"
#: FR-037 — admitting the spawn would exceed the Concurrency Ceiling hard bound.
CONCURRENCY_CEILING_EXCEEDED = "concurrency_ceiling_exceeded"
#: FR-036 — the global Kill-Switch is engaged; all Dispatch is halted.
KILL_SWITCH_ENGAGED = "kill_switch_engaged"

#: The closed set of refusal reasons a :class:`DispatchRefusal` may carry.
REFUSAL_REASONS = frozenset(
    {
        READ_ONLY_INVARIANT_VIOLATION,
        CONCURRENCY_CEILING_EXCEEDED,
        KILL_SWITCH_ENGAGED,
    }
)


# --- Value objects -----------------------------------------------------------


@dataclass(frozen=True)
class BlastRadiusScope:
    """A Project's recorded filesystem + MCP boundaries (Spec v1.3 §5.2 FR-003).

    The set of paths a Project's orchestrator Run is confined to: ``read_only_paths``
    (read but never written — must include the §9.2 FR-034 corpus path), the
    ``writable_paths`` it owns, and the ``mcp_roots`` it may reach. A frozen value
    object — :func:`provision_blast_radius` returns it unchanged (FR-035: exactly
    the recorded scope, no broader).
    """

    read_only_paths: frozenset[str]
    writable_paths: frozenset[str]
    mcp_roots: frozenset[str] = frozenset()
    # The Project's own design zone (Spec §9.2 FR-034: "and the Project's own design
    # zone"). When declared, the read-only invariant also requires it read-only.
    design_zone: str | None = None

    def owned_substrate(self) -> frozenset[str]:
        """The substrate this scope can mutate or reach — its writable paths and MCP
        roots. The read-only corpus is shared by every Project and is excluded from
        ownership, so it never counts against :func:`scopes_disjoint`."""
        return self.writable_paths | self.mcp_roots


@dataclass(frozen=True)
class DispatchRefusal:
    """The single canonical refusal shape (Spec v1.3 §9.2).

    Carries a structured ``reason`` (one of :data:`REFUSAL_REASONS`) and a
    human-readable ``detail``. The §9.3 ``requested_by`` label, when present, is
    recorded in ``detail`` only — it never influenced the decision.
    """

    reason: str
    detail: str


@dataclass(frozen=True)
class DispatchDecision:
    """The result of the pre-Dispatch hard floor (:func:`check_dispatch_allowed`).

    Either an allow (``allowed`` true, no ``refusal``) or a refusal (``allowed``
    false, carrying a :class:`DispatchRefusal`). Truthy iff allowed, so callers may
    write ``if decision:`` / ``if not decision:`` at the Dispatch boundary.
    """

    allowed: bool
    refusal: DispatchRefusal | None = None

    def __bool__(self) -> bool:
        return self.allowed

    @classmethod
    def allow(cls) -> DispatchDecision:
        """An allow decision — every invariant held."""
        return cls(allowed=True)

    @classmethod
    def refuse(cls, reason: str, detail: str) -> DispatchDecision:
        """A refusal decision carrying one canonical ``reason`` (§9.2)."""
        if reason not in REFUSAL_REASONS:
            raise ValueError(
                f"unknown refusal reason {reason!r}; "
                f"expected one of {sorted(REFUSAL_REASONS)}"
            )
        return cls(allowed=False, refusal=DispatchRefusal(reason=reason, detail=detail))


@dataclass(frozen=True)
class SafetyEscalation:
    """A top-tier escalation raised when a safety-gate trips (Spec v1.3 §9.2 FR-038).

    Returned by :func:`trip_to_paused_safety`. ``tier`` is always
    :data:`ESCALATION_TIER_TOP` (§8 FR-029 — bypasses batching + quiet hours);
    ``lifecycle_state`` is always :data:`PAUSED_SAFETY` (the Project is paused, not
    killed); ``killed`` is always ``False`` — there is no kill/terminate path. The
    §8 FR-029 surfacing/routing of this record is OLB-10 (this module returns it,
    does not route it).
    """

    project_id: str
    reason: str
    lifecycle_state: str = PAUSED_SAFETY
    tier: str = ESCALATION_TIER_TOP
    killed: bool = False


# --- Kill-Switch state primitive (FR-036) ------------------------------------


class KillSwitch:
    """The global Kill-Switch state primitive (Spec v1.3 §9.2 FR-036).

    An engage/disengage flag holder. While engaged, :func:`check_dispatch_allowed`
    refuses ALL Dispatch, overriding scheduler/admission state (§9.3 precedence).
    OLB-06 ships the STATE only; the live signalling of every ``running`` Run to a
    safe stop and the end-to-end kill-switch drill are deferred to OLB-09 (fleet
    signal) and OLB-16 (C5 drill).
    """

    def __init__(self, *, engaged: bool = False) -> None:
        self._engaged = engaged

    @property
    def engaged(self) -> bool:
        """True iff the Kill-Switch is currently engaged (all Dispatch halted)."""
        return self._engaged

    def engage(self) -> None:
        """Engage the Kill-Switch — halt all further Dispatch."""
        self._engaged = True

    def disengage(self) -> None:
        """Disengage the Kill-Switch — restore normal gate evaluation."""
        self._engaged = False


# --- The pre-Dispatch hard floor (§9.2 / §9.3) -------------------------------


def check_dispatch_allowed(
    *,
    blast_radius_scope: BlastRadiusScope,
    running_count: int,
    kill_switch: KillSwitch,
    concurrency_ceiling: int = DEFAULT_CONCURRENCY_CEILING,
    requested_by: str | None = None,
) -> DispatchDecision:
    """Apply the §9 hard floor before any autonomous Dispatch — a PURE decision.

    Refuses, in §9.3-precedence order, when:

    * the ``kill_switch`` is engaged (FR-036) — overrides everything;
    * ``blast_radius_scope`` does not list the read-only corpus path as read-only
      (FR-034) — reason :data:`READ_ONLY_INVARIANT_VIOLATION`;
    * admitting would make ``running_count + 1 > concurrency_ceiling`` (FR-037, the
      hard bound — distinct from OLB-07's FR-019 admission precondition).

    Otherwise allows. Writes nothing to any substrate. ``requested_by`` is an
    optional informational label (scheduler / admission / repair); it is recorded
    in the refusal ``detail`` ONLY and never affects the decision — so the refusal
    is structurally independent of the requesting component (§9.3 precedence).
    """
    requester = requested_by or "unspecified"

    if kill_switch.engaged:
        return DispatchDecision.refuse(
            KILL_SWITCH_ENGAGED,
            f"Kill-Switch engaged — all Dispatch halted (requested_by={requester}).",
        )

    if not lists_read_only_corpus(blast_radius_scope):
        return DispatchDecision.refuse(
            READ_ONLY_INVARIANT_VIOLATION,
            f"Blast-Radius Scope does not list {READ_ONLY_CORPUS_PATH!r} "
            f"(and any declared design zone) as read-only "
            f"(requested_by={requester}).",
        )

    if running_count + 1 > concurrency_ceiling:
        return DispatchDecision.refuse(
            CONCURRENCY_CEILING_EXCEEDED,
            f"spawn would exceed Concurrency Ceiling "
            f"({running_count}+1 > {concurrency_ceiling}) "
            f"(requested_by={requester}).",
        )

    return DispatchDecision.allow()


def lists_read_only_corpus(scope: BlastRadiusScope) -> bool:
    """True iff ``scope`` lists the read-only corpus path — and any declared design
    zone — as read-only (Spec v1.3 §9.2 FR-034).

    A path is covered when some entry in ``scope.read_only_paths`` is equal to, or an
    ancestor of, it (so a broader read-only root that contains the corpus satisfies
    the invariant). The corpus path is always required; the Project's own design
    zone is additionally required when the scope declares one.
    """
    required = [READ_ONLY_CORPUS_PATH]
    if scope.design_zone:
        required.append(scope.design_zone)
    return all(
        any(_is_ancestor_or_equal(declared, target) for declared in scope.read_only_paths)
        for target in required
    )


# --- Blast-radius scoping decision primitives (FR-035) -----------------------


def provision_blast_radius(scope: BlastRadiusScope) -> BlastRadiusScope:
    """Return exactly the recorded ``scope`` and no broader (Spec v1.3 §9.2 FR-035).

    The scoping DECISION primitive: it neither widens the read-only set nor adds a
    writable path or MCP root — it returns the recorded scope unchanged (the frozen
    value object is immutable, so the result cannot be broadened after the fact).
    The actual MCP-root provisioning AT spawn is OLB-07 admit-and-spawn; this
    establishes the "exactly its recorded scope" contract the spawn path enforces.
    """
    return scope


def scopes_disjoint(a: BlastRadiusScope, b: BlastRadiusScope) -> bool:
    """True iff no Run under scope ``a`` can reach scope ``b``'s substrate, or vice
    versa (Spec v1.3 §9.2 FR-035).

    Compares the *owned* substrate of each scope (writable paths + MCP roots; the
    shared read-only corpus is excluded). Two paths overlap when either is an
    ancestor of the other, so nested substrate (``K:/x`` vs ``K:/x/y``) is caught,
    not only exact duplicates.
    """
    return not any(
        _paths_overlap(pa, pb)
        for pa in a.owned_substrate()
        for pb in b.owned_substrate()
    )


# --- The single FR-038 trip (the only substrate write in this module) --------


def trip_to_paused_safety(
    port: RegistryPort, project_id: str, reason: str
) -> SafetyEscalation:
    """Trip a single Run into ``paused_safety`` and raise a top-tier escalation
    (Spec v1.3 §9.2 FR-038) — NEVER a kill.

    Moves the Project to :data:`PAUSED_SAFETY` through the OLB-02 write seam
    (``port.set_lifecycle_state``) — the only substrate write this module performs —
    and returns a top-tier :class:`SafetyEscalation`. No ``record_run`` /
    ``update_run_status`` is invoked and no terminal ``complete`` / ``failed`` state
    is written: the Run is paused pending operator decision, not terminated. The §8
    FR-029 surfacing/routing of the returned record is OLB-10.
    """
    port.set_lifecycle_state(project_id, PAUSED_SAFETY)
    return SafetyEscalation(project_id=project_id, reason=reason)


# --- Path helpers ------------------------------------------------------------


def _normalize(path: str) -> str:
    """Normalize a path for boundary comparison: forward slashes, no trailing
    separator, case-folded (the build runs on Windows, where paths are
    case-insensitive)."""
    return path.replace("\\", "/").rstrip("/").lower()


def _is_ancestor_or_equal(ancestor: str, target: str) -> bool:
    """True iff ``ancestor`` is equal to, or a path-prefix ancestor of, ``target``."""
    na = _normalize(ancestor)
    nt = _normalize(target)
    return nt == na or nt.startswith(na + "/")


def _paths_overlap(a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` name overlapping substrate — equal, or one an
    ancestor of the other."""
    return _is_ancestor_or_equal(a, b) or _is_ancestor_or_equal(b, a)
