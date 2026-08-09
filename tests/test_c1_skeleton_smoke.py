"""C1 — skeleton smoke integration checkpoint (OLB-05; Spec v1.3 §4.4, §13).

The first integration_checkpoint of the ol-build sequence. Where the
component suites exercise each piece in isolation, C1 wires the three built
Supervisor components together and proves them end-to-end over a SINGLE shared
zero-candidate registry:

* the OLB-01 six-step cycle host (``supervisor/cycle.py``,
  ``SupervisionCycle.run_once()``),
* the OLB-02 Registry read seam (``RegistryPort`` from ``supervisor/ports.py``),
* the OLB-04 read-only Status Surface (``supervisor/status_surface.py``).

OLB-05 register predicate — "Full six-step cycle over zero-candidate registry
logs clean idle; no writes; thin surface reflects state" — broken into the four
facets asserted below:

(a) six-step execution      — one ``run_once()`` runs reconcile -> admit ->
                              schedule -> attend -> guard -> learn, in order;
(b) clean idle log          — the pass returns None, raises nothing, and emits
                              no WARNING-or-worse log record;
(c) write-nothing           — the cycle invokes no ``RegistryPort`` WRITE method
                              (the OLB-05 core predicate, NFR-006 sole-writer);
(d) thin surface reflects   — ``build_fleet_snapshot`` / ``render_snapshot`` over
    the empty fleet            the same zero-candidate port render zero rows,
                              zero rollups, headroom == ceiling, a visible
                              ``as of`` timestamp (FR-061) — and themselves write
                              nothing.

DB-free / hermetic per gate ``olb05-c1-smoke-substrate`` option A: the substrate
is a call-recording in-memory fake ``RegistryPort`` seeded with ZERO candidate
and ZERO running rows. The live Supabase dev-branch read path is C2/OLB-08. No
built component is edited — the smoke wires their existing public surfaces only.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from supervisor.cycle import SupervisionCycle
from supervisor.ports import RegistryPort, RegistryRow
from supervisor.status_surface import (
    DEFAULT_CONCURRENCY_CEILING,
    build_fleet_snapshot,
    render_snapshot,
)

# The six §4.4 supervision steps, in their normative order (Spec v1.3 §4.4).
EXPECTED_ORDER = ["reconcile", "admit", "schedule", "attend", "guard", "learn"]

# The RegistryPort read / write method split (Spec v1.3 §5.5). The write-nothing
# probe asserts every recorded call is a read and none of the writes ever fires.
READ_METHODS = frozenset({"read_candidates", "read_running"})
WRITE_METHODS = frozenset(
    {
        "set_lifecycle_state",
        "record_run",
        "update_run_status",
        "reconcile_run",
        "set_run_orchestrator_pid",
    }
)

# A fixed build instant so the FR-061 freshness assertion is deterministic.
NOW = datetime(2026, 6, 5, 12, 30, 0, tzinfo=UTC)


class _RecordingZeroRegistry:
    """A call-recording, ZERO-candidate :class:`RegistryPort` fake.

    The substrate the whole C1 assembly runs against (gate
    ``olb05-c1-smoke-substrate`` option A). ``read_candidates`` / ``read_running``
    return empty sequences (the zero-candidate registry); every method — read or
    write — appends its own name to ``calls`` so a test can assert the wired
    cycle + surface touched ONLY read methods (the write-nothing predicate). The
    write methods are present for structural ``RegistryPort`` conformance but
    mutate nothing.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def read_candidates(self) -> Sequence[RegistryRow]:
        self.calls.append("read_candidates")
        return []

    def read_running(self) -> Sequence[RegistryRow]:
        self.calls.append("read_running")
        return []

    def set_lifecycle_state(self, project_id: str, state: str) -> None:
        self.calls.append("set_lifecycle_state")

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


@pytest.fixture
def port() -> _RecordingZeroRegistry:
    """A fresh call-recording zero-candidate registry per test."""
    return _RecordingZeroRegistry()


def _spy_step_order(
    cycle: SupervisionCycle,
    monkeypatch: pytest.MonkeyPatch,
    recorder: list[str],
) -> None:
    """Wrap each §4.4 step hook to record its name, then CALL THROUGH to the real
    hook — so the genuine no-op stubs still run (true integration) while the
    invocation order is observed (the step-hook observable, Spec §4.4)."""
    for step in EXPECTED_ORDER:
        original = getattr(cycle, f"_{step}")

        def make_wrapper(step_name: str, real):
            def wrapper() -> None:
                recorder.append(step_name)
                return real()

            return wrapper

        monkeypatch.setattr(cycle, f"_{step}", make_wrapper(step, original))


# --- structural conformance: the zero-candidate fake is a real RegistryPort ---


@pytest.mark.integration
def test_zero_registry_is_a_structural_registry_port(
    port: _RecordingZeroRegistry,
) -> None:
    """The C1 substrate satisfies the OLB-02 RegistryPort seam — the cycle host
    and the surface depend only on this Protocol (ports.py untouched)."""
    assert isinstance(port, RegistryPort)


# --- (a) six-step execution ---


@pytest.mark.integration
def test_full_cycle_executes_six_steps_in_section_4_4_order(
    port: _RecordingZeroRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Facet (a): one wired ``run_once()`` over the zero-candidate registry runs
    all six §4.4 steps, in order reconcile -> admit -> schedule -> attend ->
    guard -> learn. Each is a no-op over zero candidates, but all six still run."""
    cycle = SupervisionCycle(port)
    order: list[str] = []
    _spy_step_order(cycle, monkeypatch, order)

    cycle.run_once()

    assert order == EXPECTED_ORDER


# --- (b) clean idle log ---


@pytest.mark.integration
def test_full_cycle_logs_clean_idle_pass(
    port: _RecordingZeroRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    """Facet (b): the zero-candidate pass is a clean idle — it returns None,
    raises nothing, and emits no WARNING-or-worse log record. Capturing the
    records here catches a future regression that logs an anomaly on an idle
    pass."""
    cycle = SupervisionCycle(port)

    with caplog.at_level(logging.WARNING):
        result = cycle.run_once()

    assert result is None
    anomalies = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert anomalies == [], f"idle pass emitted anomalies: {[r.message for r in anomalies]}"


# --- (c) write-nothing probe (the OLB-05 core predicate) ---


@pytest.mark.integration
def test_full_cycle_writes_nothing_on_zero_candidate(
    port: _RecordingZeroRegistry,
) -> None:
    """Facet (c) — the OLB-05 core predicate: a full ``run_once()`` over the
    zero-candidate registry invokes NONE of the RegistryPort write methods
    (set_lifecycle_state / record_run / update_run_status). Every recorded call,
    if any, is a read (NFR-006 sole-writer — the cycle never writes)."""
    cycle = SupervisionCycle(port)

    cycle.run_once()

    assert WRITE_METHODS.isdisjoint(port.calls)
    assert all(call in READ_METHODS for call in port.calls), port.calls


# --- (d) thin surface reflects the empty-fleet state ---


@pytest.mark.integration
def test_thin_surface_reflects_empty_fleet(port: _RecordingZeroRegistry) -> None:
    """Facet (d): ``build_fleet_snapshot`` / ``render_snapshot`` over the same
    zero-candidate port render the empty fleet — zero rows, zero rollups,
    headroom == ceiling (no running Runs), a visible ``as of`` timestamp
    (FR-061) — and the surface itself writes nothing."""
    snapshot = build_fleet_snapshot(port, now=NOW)
    rendered = render_snapshot(snapshot)

    assert snapshot.rows == ()
    assert snapshot.counts_by_lifecycle_state == {}
    assert snapshot.total_attention_debt == 0
    assert snapshot.running_count == 0
    assert snapshot.concurrency_ceiling == DEFAULT_CONCURRENCY_CEILING
    assert snapshot.headroom == DEFAULT_CONCURRENCY_CEILING  # full headroom, no Runs

    assert "as of" in rendered
    assert NOW.isoformat() in rendered
    assert "Projects: 0" in rendered

    assert WRITE_METHODS.isdisjoint(port.calls)


# --- C1 capstone: the four facets over ONE shared zero-candidate substrate ---


@pytest.mark.integration
def test_one_full_cycle_then_surface_over_shared_zero_registry(
    port: _RecordingZeroRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The C1 assembly proof: the OLB-01 cycle host AND the OLB-04 surface, wired
    to a SINGLE shared zero-candidate RegistryPort, run end-to-end in one pass —

    * (a) all six §4.4 steps execute in order,
    * (b) the cycle returns None and raises nothing (clean idle),
    * (c) the wired system (cycle + surface) invokes NO write method, and
    * (d) the surface renders the empty fleet (zero rows, full headroom).

    This is what C1 adds over the per-component suites: the three components
    proven together against one substrate, writing nothing."""
    cycle = SupervisionCycle(port)
    order: list[str] = []
    _spy_step_order(cycle, monkeypatch, order)

    result = cycle.run_once()
    rendered = render_snapshot(build_fleet_snapshot(port, now=NOW))

    # (a) + (b): six steps in order, clean idle return.
    assert order == EXPECTED_ORDER
    assert result is None
    # (c): across BOTH the cycle and the surface, not one write method fired.
    assert WRITE_METHODS.isdisjoint(port.calls)
    assert all(call in READ_METHODS for call in port.calls), port.calls
    # (d): the thin surface reflects the empty fleet.
    assert "Projects: 0" in rendered
    assert f"Headroom: {DEFAULT_CONCURRENCY_CEILING}" in rendered
    assert NOW.isoformat() in rendered
