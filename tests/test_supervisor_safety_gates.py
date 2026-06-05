"""Component tests for the OLB-06 Safety-Gates hard floor (``supervisor/safety_gates.py``).

Covers the OLB-06 predicate (Spec v1.3 §9): the §9 invariants are enforced
independently of any requesting component (§9.3 precedence) —

* (a) FR-034 read-only-corpus invariant — a Dispatch whose Blast-Radius Scope omits
  the read-only corpus path is refused with reason ``read_only_invariant_violation``;
  a scope that lists it as read-only is allowed.
* (b) FR-037 concurrency-ceiling — at the ceiling any spawn is refused as a hard
  bound; below it a spawn is allowed; the refusal is independent of the requester.
* (c) FR-038 trip-to-``paused_safety`` — ``trip_to_paused_safety`` writes
  ``paused_safety`` exactly once, returns a top-tier escalation, and NEVER kills
  (no ``record_run`` / ``update_run_status`` / terminal ``complete`` / ``failed``).
* (d) FR-036 Kill-Switch — engaged => every Dispatch refused regardless of
  scope/headroom; disengaged => normal evaluation.
* (e) FR-035 scoping — ``provision_blast_radius`` returns exactly the recorded scope
  (no broadening); ``scopes_disjoint`` is true for disjoint scopes, false for
  overlapping ones.

DB-free / hermetic: the one substrate write (the FR-038 trip) is exercised through
a CALL-RECORDING fake ``RegistryPort`` — a dict-backed double that appends every
method name it receives to a ``calls`` list and records each lifecycle write — so
the never-kill predicate is asserted against the actual shipped trip, not a
parallel fake. Every other primitive is pure and needs no port.
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import pytest

from supervisor.ports import RegistryPort, RegistryRow
from supervisor.safety_gates import (
    CONCURRENCY_CEILING_EXCEEDED,
    DEFAULT_CONCURRENCY_CEILING,
    ESCALATION_TIER_TOP,
    KILL_SWITCH_ENGAGED,
    PAUSED_SAFETY,
    READ_ONLY_CORPUS_PATH,
    READ_ONLY_INVARIANT_VIOLATION,
    REFUSAL_REASONS,
    BlastRadiusScope,
    KillSwitch,
    check_dispatch_allowed,
    lists_read_only_corpus,
    provision_blast_radius,
    scopes_disjoint,
    trip_to_paused_safety,
)

# The two RegistryPort run-write methods. The never-kill probe asserts neither ever
# appears in the fake's recorded calls during an FR-038 trip (the Run is paused, not
# reconciled to a terminal Run status).
RUN_WRITE_METHODS = frozenset({"record_run", "update_run_status"})

# Terminal lifecycle states a kill would write. The FR-038 trip must write none of
# them — only ``paused_safety``.
TERMINAL_STATES = frozenset({"complete", "failed"})


# --- A call-recording fake RegistryPort: records every call + every lifecycle write ---


class _RecordingRegistryPort:
    """Dict-backed :class:`RegistryPort` double.

    Appends the NAME of every method invoked to ``calls`` and records each
    ``set_lifecycle_state`` write as a ``(project_id, state)`` pair in
    ``lifecycle_writes`` — so a test can assert the FR-038 trip wrote
    ``paused_safety`` exactly once and invoked no kill path.
    """

    def __init__(
        self,
        candidates: Sequence[RegistryRow] = (),
        running: Sequence[RegistryRow] = (),
    ) -> None:
        self._candidates = list(candidates)
        self._running = list(running)
        self.calls: list[str] = []
        self.lifecycle_writes: list[tuple[str, str]] = []

    def read_candidates(self) -> Sequence[RegistryRow]:
        self.calls.append("read_candidates")
        return list(self._candidates)

    def read_running(self) -> Sequence[RegistryRow]:
        self.calls.append("read_running")
        return list(self._running)

    def set_lifecycle_state(self, project_id: str, state: str) -> None:
        self.calls.append("set_lifecycle_state")
        self.lifecycle_writes.append((project_id, state))

    def record_run(self, project_id: str, run: RegistryRow) -> None:
        self.calls.append("record_run")

    def update_run_status(self, project_id: str, status: str) -> None:
        self.calls.append("update_run_status")

    def reconcile_run(
        self,
        project_id: str,
        status: str,
        *,
        terminated_at: str,
        terminal_cost_usd: Decimal,
    ) -> None:
        self.calls.append("reconcile_run")

    def set_run_orchestrator_pid(
        self, project_id: str, orchestrator_pid: int
    ) -> None:
        self.calls.append("set_run_orchestrator_pid")


def _scope(
    *,
    read_only: Sequence[str] = (READ_ONLY_CORPUS_PATH,),
    writable: Sequence[str] = ("K:/work/projA",),
    mcp_roots: Sequence[str] = (),
    design_zone: str | None = None,
) -> BlastRadiusScope:
    """A Blast-Radius Scope; defaults list the read-only corpus path (FR-034 compliant)."""
    return BlastRadiusScope(
        read_only_paths=frozenset(read_only),
        writable_paths=frozenset(writable),
        mcp_roots=frozenset(mcp_roots),
        design_zone=design_zone,
    )


# A scope that lists the read-only corpus path (FR-034 compliant) and one that omits it.
COMPLIANT_SCOPE = _scope()
NON_COMPLIANT_SCOPE = _scope(read_only=())


@pytest.fixture
def port() -> _RecordingRegistryPort:
    """A fresh call-recording fake per test."""
    return _RecordingRegistryPort()


# --- structural conformance: the fake is a real RegistryPort ---


@pytest.mark.unit
def test_recording_fake_is_a_structural_registry_port(
    port: _RecordingRegistryPort,
) -> None:
    """The fake satisfies the OLB-02 RegistryPort seam (the gate consumes only this
    Protocol's read methods + the single ``set_lifecycle_state`` trip; ports.py
    untouched)."""
    assert isinstance(port, RegistryPort)


# --- (a) FR-034 read-only-corpus invariant refusal ---


@pytest.mark.unit
def test_fr034_scope_omitting_read_only_corpus_is_refused() -> None:
    """FR-034: a Dispatch whose Blast-Radius Scope omits the read-only corpus path is
    refused with reason ``read_only_invariant_violation``."""
    decision = check_dispatch_allowed(
        blast_radius_scope=NON_COMPLIANT_SCOPE,
        running_count=0,
        kill_switch=KillSwitch(),
    )

    assert not decision
    assert decision.refusal is not None
    assert decision.refusal.reason == READ_ONLY_INVARIANT_VIOLATION


@pytest.mark.unit
def test_fr034_scope_listing_read_only_corpus_is_allowed() -> None:
    """FR-034: a scope that lists the read-only corpus path as read-only (with
    headroom and the Kill-Switch disengaged) is allowed."""
    decision = check_dispatch_allowed(
        blast_radius_scope=COMPLIANT_SCOPE,
        running_count=0,
        kill_switch=KillSwitch(),
    )

    assert decision
    assert decision.refusal is None


@pytest.mark.unit
def test_fr034_declared_design_zone_must_also_be_read_only() -> None:
    """FR-034: when the scope declares the Project's own design zone, the invariant
    requires it read-only too — corpus alone is not enough."""
    corpus_only = _scope(read_only=(READ_ONLY_CORPUS_PATH,), design_zone="K:/work/projA/design")
    both = _scope(
        read_only=(READ_ONLY_CORPUS_PATH, "K:/work/projA/design"),
        design_zone="K:/work/projA/design",
    )

    assert not lists_read_only_corpus(corpus_only)
    assert lists_read_only_corpus(both)


# --- (b) FR-037 concurrency-ceiling refusal ---


@pytest.mark.unit
def test_fr037_spawn_at_ceiling_is_refused() -> None:
    """FR-037: at the ceiling (=2) any spawn request is refused as a hard bound."""
    decision = check_dispatch_allowed(
        blast_radius_scope=COMPLIANT_SCOPE,
        running_count=DEFAULT_CONCURRENCY_CEILING,
        kill_switch=KillSwitch(),
    )

    assert not decision
    assert decision.refusal is not None
    assert decision.refusal.reason == CONCURRENCY_CEILING_EXCEEDED


@pytest.mark.unit
def test_fr037_spawn_below_ceiling_is_allowed() -> None:
    """FR-037: below the ceiling a spawn is allowed (the bound refuses only the spawn
    that would exceed it)."""
    decision = check_dispatch_allowed(
        blast_radius_scope=COMPLIANT_SCOPE,
        running_count=DEFAULT_CONCURRENCY_CEILING - 1,
        kill_switch=KillSwitch(),
    )

    assert decision


@pytest.mark.unit
@pytest.mark.parametrize("requester", ["scheduler", "admission", "repair", None])
def test_fr037_ceiling_refusal_is_independent_of_requester(requester: str | None) -> None:
    """§9.3 precedence: the ceiling refusal fires identically no matter which
    component requested the spawn — the ``requested_by`` label never changes the
    decision."""
    decision = check_dispatch_allowed(
        blast_radius_scope=COMPLIANT_SCOPE,
        running_count=DEFAULT_CONCURRENCY_CEILING,
        kill_switch=KillSwitch(),
        requested_by=requester,
    )

    assert not decision
    assert decision.refusal is not None
    assert decision.refusal.reason == CONCURRENCY_CEILING_EXCEEDED


# --- (c) FR-038 trip-to-paused_safety, never kill ---


@pytest.mark.unit
def test_fr038_trip_moves_to_paused_safety_and_returns_top_tier_escalation(
    port: _RecordingRegistryPort,
) -> None:
    """FR-038: ``trip_to_paused_safety`` writes ``paused_safety`` exactly once via the
    OLB-02 seam and returns a top-tier escalation record."""
    escalation = trip_to_paused_safety(port, "projA", "blast_radius_breach")

    assert port.lifecycle_writes == [("projA", PAUSED_SAFETY)]
    assert port.calls.count("set_lifecycle_state") == 1
    assert escalation.project_id == "projA"
    assert escalation.reason == "blast_radius_breach"
    assert escalation.lifecycle_state == PAUSED_SAFETY
    assert escalation.tier == ESCALATION_TIER_TOP


@pytest.mark.unit
def test_fr038_trip_never_kills_the_run(port: _RecordingRegistryPort) -> None:
    """FR-038: the trip pauses the Run, never kills it — no ``record_run`` /
    ``update_run_status`` call, no terminal ``complete`` / ``failed`` write, and the
    escalation is explicitly not a kill."""
    escalation = trip_to_paused_safety(port, "projA", "blast_radius_breach")

    assert RUN_WRITE_METHODS.isdisjoint(port.calls)
    written_states = {state for _, state in port.lifecycle_writes}
    assert TERMINAL_STATES.isdisjoint(written_states)
    assert escalation.killed is False


# --- (d) FR-036 Kill-Switch ---


@pytest.mark.unit
def test_fr036_engaged_kill_switch_refuses_every_dispatch() -> None:
    """FR-036: with the Kill-Switch engaged, a Dispatch is refused even when the scope
    is compliant and there is full headroom."""
    decision = check_dispatch_allowed(
        blast_radius_scope=COMPLIANT_SCOPE,
        running_count=0,
        kill_switch=KillSwitch(engaged=True),
    )

    assert not decision
    assert decision.refusal is not None
    assert decision.refusal.reason == KILL_SWITCH_ENGAGED


@pytest.mark.unit
def test_fr036_engaged_kill_switch_overrides_other_refusals() -> None:
    """§9.3 precedence: the Kill-Switch overrides everything — engaged + non-compliant
    scope + over ceiling still reports the Kill-Switch reason (it is checked first)."""
    decision = check_dispatch_allowed(
        blast_radius_scope=NON_COMPLIANT_SCOPE,
        running_count=DEFAULT_CONCURRENCY_CEILING + 5,
        kill_switch=KillSwitch(engaged=True),
    )

    assert not decision
    assert decision.refusal is not None
    assert decision.refusal.reason == KILL_SWITCH_ENGAGED


@pytest.mark.unit
def test_fr036_disengage_restores_normal_evaluation() -> None:
    """FR-036: engaging then disengaging the Kill-Switch restores normal gate
    evaluation (a compliant Dispatch with headroom is allowed again)."""
    kill_switch = KillSwitch()
    kill_switch.engage()
    assert kill_switch.engaged
    assert not check_dispatch_allowed(
        blast_radius_scope=COMPLIANT_SCOPE, running_count=0, kill_switch=kill_switch
    )

    kill_switch.disengage()
    assert not kill_switch.engaged
    assert check_dispatch_allowed(
        blast_radius_scope=COMPLIANT_SCOPE, running_count=0, kill_switch=kill_switch
    )


# --- (e) FR-035 scoping-decision primitives ---


@pytest.mark.unit
def test_fr035_provision_returns_exactly_the_recorded_scope() -> None:
    """FR-035: ``provision_blast_radius`` returns exactly the recorded scope and no
    broader — the frozen value object is returned unchanged."""
    provisioned = provision_blast_radius(COMPLIANT_SCOPE)

    assert provisioned == COMPLIANT_SCOPE
    assert provisioned is COMPLIANT_SCOPE
    assert provisioned.read_only_paths == COMPLIANT_SCOPE.read_only_paths
    assert provisioned.writable_paths == COMPLIANT_SCOPE.writable_paths


@pytest.mark.unit
def test_fr035_disjoint_scopes_are_disjoint() -> None:
    """FR-035: two Projects with non-overlapping owned substrate are disjoint — neither
    can reach the other's writable/MCP roots."""
    a = _scope(writable=("K:/work/projA",), mcp_roots=("mcp://projA",))
    b = _scope(writable=("K:/work/projB",), mcp_roots=("mcp://projB",))

    assert scopes_disjoint(a, b)


@pytest.mark.unit
def test_fr035_overlapping_scopes_are_not_disjoint() -> None:
    """FR-035: a nested writable path (one scope's substrate inside another's) is NOT
    disjoint — overlap is caught by ancestor containment, not only exact match."""
    a = _scope(writable=("K:/work/projA",))
    nested = _scope(writable=("K:/work/projA/sub",))

    assert not scopes_disjoint(a, nested)


@pytest.mark.unit
def test_fr035_shared_read_only_corpus_does_not_break_disjointness() -> None:
    """FR-035: the read-only corpus is shared by every Project, so two scopes that both
    list it as read-only are still disjoint when their owned substrate differs."""
    a = _scope(writable=("K:/work/projA",))
    b = _scope(writable=("K:/work/projB",))

    assert scopes_disjoint(a, b)


# --- canonical refusal shape ---


@pytest.mark.unit
def test_every_refusal_carries_a_canonical_reason_code() -> None:
    """Every refusal the gate can produce carries one of the closed
    :data:`REFUSAL_REASONS` codes — no ad-hoc refusal strings."""
    refusals = [
        check_dispatch_allowed(
            blast_radius_scope=COMPLIANT_SCOPE,
            running_count=0,
            kill_switch=KillSwitch(engaged=True),
        ),
        check_dispatch_allowed(
            blast_radius_scope=NON_COMPLIANT_SCOPE,
            running_count=0,
            kill_switch=KillSwitch(),
        ),
        check_dispatch_allowed(
            blast_radius_scope=COMPLIANT_SCOPE,
            running_count=DEFAULT_CONCURRENCY_CEILING,
            kill_switch=KillSwitch(),
        ),
    ]

    for decision in refusals:
        assert not decision
        assert decision.refusal is not None
        assert decision.refusal.reason in REFUSAL_REASONS
