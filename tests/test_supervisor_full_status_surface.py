"""Component tests for the OLB-16 Full Status Surface (``supervisor/full_status_surface.py``).

Covers the OLB-16 predicate (Spec v1.3 §13): the read-only projection renders the
FULL FR-059 five-field per-Project row (lifecycle_state + active-Run status +
Attention Debt + open work-registry count + cumulative cost) — the two fields
OLB-04 forward-referenced here — plus the FR-062 stale-heartbeat distinction, the
FR-063 non-binding cost framing, and the FR-061 bounded refresh-interval / as-of
timestamp, and the NFR-009 / FR-058 write-nothing probe passes (no row mutation
observed: the full build's calls are a subset of the read set).

DB-free / hermetic: driven through the same CALL-RECORDING fake ``RegistryPort`` the
OLB-04 suite uses — a dict-backed double that appends every method name it receives to
a ``calls`` list and serves canned ``projects`` rows. The two FR-059 fields + the
FR-062 heartbeat instants are supplied as injected per-Project maps (the live wiring's
source, the thin ``main()`` reader, is NOT under closure test per gate
``olb16-status-surface-tui-form-and-scope`` = A).
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from supervisor.full_status_surface import (
    COST_NON_BINDING_NOTE,
    DEFAULT_HEARTBEAT_STALE_AFTER,
    HEARTBEAT_HEALTHY,
    HEARTBEAT_NA,
    HEARTBEAT_STALLED,
    RefreshScheduler,
    build_full_fleet_snapshot,
    render_full_snapshot,
)
from supervisor.ports import RegistryPort, RegistryRow
from supervisor.status_surface import ACTIVE_RUN_NONE, ACTIVE_RUN_RUNNING

# The three RegistryPort write methods (Spec v1.3 §5.5). The write-nothing probe
# asserts none of these ever appears in the fake's recorded calls.
WRITE_METHODS = frozenset({"set_lifecycle_state", "record_run", "update_run_status"})

# A fixed build instant so the FR-061 freshness + FR-062 staleness assertions are
# deterministic (no wall-clock read in the pure core — ``now`` is injected).
NOW = datetime(2026, 6, 5, 12, 30, 0)


def _project_row(
    project_id: str, display_name: str, lifecycle_state: str, attention_debt: int
) -> dict[str, object]:
    """A ``projects`` row shaped like the OLB-02 read seam returns (PROJECT_COLUMNS)."""
    return {
        "project_id": project_id,
        "display_name": display_name,
        "folder_path": f"/{project_id}",
        "kind": "initiative",
        "status": "active",
        "lifecycle_state": lifecycle_state,
        "priority": 100,
        "blast_radius_scope": None,
        "attention_debt": attention_debt,
        "heartbeat_workstream_id": None,
    }


class _RecordingRegistryPort:
    """Dict-backed :class:`RegistryPort` double that records every method invoked.

    Serves the programmed candidate / running rows and appends the NAME of every
    method invoked to ``calls`` — so a test can assert a full build+render touched
    only the read methods (FR-058 / NFR-009). The write methods are present
    (structural conformance) but only record; they mutate nothing.
    """

    def __init__(
        self,
        candidates: Sequence[RegistryRow],
        running: Sequence[RegistryRow],
    ) -> None:
        self._candidates = list(candidates)
        self._running = list(running)
        self.calls: list[str] = []

    def read_candidates(self) -> Sequence[RegistryRow]:
        self.calls.append("read_candidates")
        return list(self._candidates)

    def read_running(self) -> Sequence[RegistryRow]:
        self.calls.append("read_running")
        return list(self._running)

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


# Two Candidates + two running Projects. One running Project has a FRESH heartbeat
# (healthy), the other a STALE one (stalled) — so the FR-062 distinction is exercised.
CANDIDATES: list[RegistryRow] = [
    _project_row("c1", "Candidate One", "candidate", 0),
    _project_row("c2", "Candidate Two", "candidate", 4),
]
RUNNING: list[RegistryRow] = [
    _project_row("r1", "Running Fresh", "running", 1),
    _project_row("r2", "Running Stale", "running", 0),
]

OPEN_WORK_COUNTS = {"c1": 3, "c2": 7, "r1": 2, "r2": 5}
CUMULATIVE_COSTS = {
    "c1": Decimal("0"),
    "c2": Decimal("1.2345"),
    "r1": Decimal("4.5000"),
    "r2": Decimal("10.0001"),
}
# r1 heartbeat is 1 minute old (fresh); r2 is 30 minutes old (> the 15m default → stale).
HEARTBEATS = {
    "r1": NOW - timedelta(minutes=1),
    "r2": NOW - timedelta(minutes=30),
}


@pytest.fixture
def port() -> _RecordingRegistryPort:
    """A fresh call-recording fake over the canned fleet per test."""
    return _RecordingRegistryPort(CANDIDATES, RUNNING)


def _snapshot(port: _RecordingRegistryPort):
    """Build the full snapshot over the canned fleet + injected supplemental maps."""
    return build_full_fleet_snapshot(
        port,
        now=NOW,
        open_work_counts=OPEN_WORK_COUNTS,
        cumulative_costs=CUMULATIVE_COSTS,
        heartbeats=HEARTBEATS,
    )


# --- structural conformance --------------------------------------------------


@pytest.mark.unit
def test_recording_fake_is_a_structural_registry_port(
    port: _RecordingRegistryPort,
) -> None:
    """The fake satisfies the OLB-02 RegistryPort seam (the surface depends only on
    this Protocol; ports.py untouched)."""
    assert isinstance(port, RegistryPort)


# --- (a) FR-059 full five-field row ------------------------------------------


@pytest.mark.unit
def test_build_returns_full_five_field_row_per_project(
    port: _RecordingRegistryPort,
) -> None:
    """FR-059: each consolidated row carries the FULL five fields — lifecycle_state,
    active_run_status, attention_debt, AND the two fields OLB-04 deferred here
    (open_work_count + cumulative_cost_usd)."""
    snapshot = _snapshot(port)

    assert len(snapshot.rows) == len(CANDIDATES) + len(RUNNING)
    by_id = {r.project_id: r for r in snapshot.rows}
    assert by_id["c2"].open_work_count == 7
    assert by_id["c2"].cumulative_cost_usd == Decimal("1.2345")
    assert by_id["r1"].lifecycle_state == "running"
    assert by_id["r1"].active_run_status == ACTIVE_RUN_RUNNING
    assert by_id["c1"].active_run_status == ACTIVE_RUN_NONE
    assert isinstance(by_id["r2"].cumulative_cost_usd, Decimal)


@pytest.mark.unit
def test_rollup_totals_open_work_and_cost(port: _RecordingRegistryPort) -> None:
    """The fleet rollup sums open work-registry count and cumulative cost across the
    fleet (exact Decimal, NFR-007) alongside the OLB-04 Attention-Debt total."""
    snapshot = _snapshot(port)

    assert snapshot.total_open_work_count == 3 + 7 + 2 + 5
    assert snapshot.total_cumulative_cost_usd == Decimal("15.7346")
    assert snapshot.total_attention_debt == 0 + 4 + 1 + 0


@pytest.mark.unit
def test_degenerate_cost_values_read_as_zero_never_raise() -> None:
    """The surface renders a partially-populated fleet WITHOUT raising: a stray bool or a
    non-finite Decimal in the cost map reads as $0 rather than crashing the whole build."""
    port = _RecordingRegistryPort(
        [_project_row("b1", "Bool Cost", "candidate", 0)],
        [_project_row("n1", "NaN Cost", "running", 0)],
    )

    snapshot = build_full_fleet_snapshot(
        port,
        now=NOW,
        cumulative_costs={"b1": True, "n1": Decimal("NaN")},  # type: ignore[dict-item]
    )

    by_id = {r.project_id: r for r in snapshot.rows}
    assert by_id["b1"].cumulative_cost_usd == Decimal("0.0000")
    assert by_id["n1"].cumulative_cost_usd == Decimal("0.0000")
    assert snapshot.total_cumulative_cost_usd == Decimal("0.0000")


@pytest.mark.unit
def test_missing_supplemental_entries_default_to_zero() -> None:
    """A Project unlisted in the injected maps reads its zero default (0 / $0) rather
    than raising — a partially-populated fleet still renders (FR-060 derive-live)."""
    port = _RecordingRegistryPort([_project_row("x1", "Bare", "candidate", 0)], [])

    snapshot = build_full_fleet_snapshot(port, now=NOW)  # no maps supplied

    (row,) = snapshot.rows
    assert row.open_work_count == 0
    assert row.cumulative_cost_usd == Decimal("0.0000")
    assert row.heartbeat_state == HEARTBEAT_NA


# --- (b) FR-062 stale-heartbeat distinction ----------------------------------


@pytest.mark.unit
def test_fr062_running_heartbeat_healthy_vs_stalled(
    port: _RecordingRegistryPort,
) -> None:
    """FR-062: a running Run with a fresh heartbeat reads HEALTHY; one whose heartbeat
    is older than the threshold reads STALLED — a visibly distinct marker."""
    by_id = {r.project_id: r for r in _snapshot(port).rows}

    assert by_id["r1"].heartbeat_state == HEARTBEAT_HEALTHY
    assert by_id["r2"].heartbeat_state == HEARTBEAT_STALLED
    assert HEARTBEAT_HEALTHY != HEARTBEAT_STALLED


@pytest.mark.unit
def test_fr062_running_with_no_heartbeat_is_stalled() -> None:
    """FR-062: a running Run that has emitted NO heartbeat at all reads STALLED (the
    worst case of stale), never a spurious healthy."""
    port = _RecordingRegistryPort([], [_project_row("r9", "No Beat", "running", 0)])

    (row,) = build_full_fleet_snapshot(port, now=NOW, heartbeats={}).rows

    assert row.active_run_status == ACTIVE_RUN_RUNNING
    assert row.heartbeat_state == HEARTBEAT_STALLED


@pytest.mark.unit
def test_fr062_non_running_project_has_no_heartbeat_marker(
    port: _RecordingRegistryPort,
) -> None:
    """FR-062: heartbeat is only meaningful for a running Run — a Candidate reads the
    not-applicable sentinel, never healthy/stalled."""
    by_id = {r.project_id: r for r in _snapshot(port).rows}

    assert by_id["c1"].heartbeat_state == HEARTBEAT_NA
    assert by_id["c2"].heartbeat_state == HEARTBEAT_NA


@pytest.mark.unit
def test_fr062_stalled_count_in_rollup(port: _RecordingRegistryPort) -> None:
    """The rollup counts stalled heartbeats (exactly r2 in the canned fleet)."""
    assert _snapshot(port).stalled_count == 1


@pytest.mark.unit
def test_fr062_threshold_is_injected_not_owned() -> None:
    """The staleness threshold is the operator's (injected): widening it past the
    heartbeat age flips a previously-stalled Run back to healthy."""
    port = _RecordingRegistryPort([], [_project_row("r2", "Running Stale", "running", 0)])
    heartbeats = {"r2": NOW - timedelta(minutes=30)}

    stalled = build_full_fleet_snapshot(port, now=NOW, heartbeats=heartbeats)
    healthy = build_full_fleet_snapshot(
        port, now=NOW, heartbeats=heartbeats, heartbeat_stale_after=timedelta(hours=1)
    )

    assert stalled.rows[0].heartbeat_state == HEARTBEAT_STALLED
    assert healthy.rows[0].heartbeat_state == HEARTBEAT_HEALTHY
    # Sanity: the default threshold is the 15-minute bound exercised above.
    assert DEFAULT_HEARTBEAT_STALE_AFTER == timedelta(minutes=15)


# --- (c) FR-058 / NFR-009 write-nothing probe (the OLB-16 read-only predicate) ---


@pytest.mark.unit
def test_build_and_render_invoke_only_read_methods(
    port: _RecordingRegistryPort,
) -> None:
    """FR-058 / NFR-009: a full build+render calls ONLY read_candidates /
    read_running — none of the three write methods is ever invoked (the surface's
    calls are a subset of the read set; no row mutation observed)."""
    render_full_snapshot(_snapshot(port))

    assert set(port.calls) <= {"read_candidates", "read_running"}
    assert WRITE_METHODS.isdisjoint(port.calls)


# --- (d) FR-061 refresh-interval / as-of -------------------------------------


@pytest.mark.unit
def test_fr061_render_carries_as_of_and_refresh_interval(
    port: _RecordingRegistryPort,
) -> None:
    """FR-061: the rendered snapshot displays the ``now`` it was built at AND the
    bounded refresh interval."""
    rendered = render_full_snapshot(_snapshot(port))

    assert NOW.isoformat() in rendered
    assert "as of" in rendered
    assert "refresh every" in rendered


@pytest.mark.unit
def test_fr061_refresh_scheduler_is_due_and_next() -> None:
    """FR-061: the bounded scheduler reports a refresh due only once the interval has
    elapsed since the last build, and computes the next-due instant."""
    scheduler = RefreshScheduler(interval=timedelta(seconds=5))
    last = NOW

    assert scheduler.next_refresh_at(last) == NOW + timedelta(seconds=5)
    assert not scheduler.is_due(last_as_of=last, now=NOW + timedelta(seconds=4))
    assert scheduler.is_due(last_as_of=last, now=NOW + timedelta(seconds=5))
    # The wait never goes negative (clamped at 0 once due).
    assert scheduler.seconds_until_due(last_as_of=last, now=NOW + timedelta(seconds=3)) == 2.0
    assert scheduler.seconds_until_due(last_as_of=last, now=NOW + timedelta(seconds=9)) == 0.0


@pytest.mark.unit
def test_fr061_refresh_scheduler_rejects_non_positive_interval() -> None:
    """A zero / negative interval is rejected — the refresh is BOUNDED (FR-061), so a
    no-wait busy loop can never be configured."""
    with pytest.raises(ValueError, match="positive"):
        RefreshScheduler(interval=timedelta(0))


# --- (e) FR-063 non-binding cost framing -------------------------------------


@pytest.mark.unit
def test_fr063_cost_is_framed_non_binding(port: _RecordingRegistryPort) -> None:
    """FR-063: the cost is rendered as operator information explicitly framed
    NON-BINDING, naming the Cost Circuit-Breaker as the enforcing authority — the
    surface enforces nothing."""
    rendered = render_full_snapshot(_snapshot(port))

    assert COST_NON_BINDING_NOTE in rendered
    assert "NON-BINDING" in rendered
    assert "Cost Circuit-Breaker" in rendered


# --- (f) golden-snapshot full-row render -------------------------------------


@pytest.mark.unit
def test_golden_snapshot_full_row_render(port: _RecordingRegistryPort) -> None:
    """A golden render: every Project's name + the full five-field row + the FR-062
    marker + the rollup totals + the FR-063 footnote all appear, in a stable layout."""
    rendered = render_full_snapshot(_snapshot(port))
    lines = rendered.splitlines()

    # Header band.
    assert lines[0] == "Outer Loop Supervisor — Full Fleet Status"
    assert lines[1].startswith(f"as of {NOW.isoformat()} | refresh every 5s")

    # One line per Project, each carrying its display name + heartbeat marker.
    for canned in CANDIDATES + RUNNING:
        assert str(canned["display_name"]) in rendered
    assert HEARTBEAT_HEALTHY in rendered
    assert HEARTBEAT_STALLED in rendered

    # Rollup band: open-work + cost + stalled-count totals are surfaced.
    assert "Open Work: 17" in rendered
    assert "$15.7346" in rendered
    assert "Stalled heartbeats: 1" in rendered


@pytest.mark.unit
def test_empty_fleet_renders_clean_and_writes_nothing() -> None:
    """A zero-Project registry yields an empty full surface: no rows, zero totals — and
    still reads (never writes)."""
    empty = _RecordingRegistryPort([], [])

    snapshot = build_full_fleet_snapshot(empty, now=NOW)
    rendered = render_full_snapshot(snapshot)

    assert snapshot.rows == ()
    assert snapshot.total_open_work_count == 0
    assert snapshot.total_cumulative_cost_usd == Decimal("0.0000")
    assert snapshot.stalled_count == 0
    assert NOW.isoformat() in rendered
    assert COST_NON_BINDING_NOTE in rendered
    assert WRITE_METHODS.isdisjoint(empty.calls)
