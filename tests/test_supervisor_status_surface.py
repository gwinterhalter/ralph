"""Component tests for the OLB-04 thin Status Surface (``supervisor/status_surface.py``).

Covers the OLB-04 predicate (Spec v1.3 §13): the read-only projection renders a
per-Project consolidated row (lifecycle_state + active-Run status + Attention
Debt) from live rows plus the fleet rollup (FR-059 thin subset, gate
``olb04-status-surface-field-scope`` option A), with a visible as-of timestamp
(FR-061) — and the NFR-009 / FR-058 write-nothing probe passes (no row mutation
observed).

DB-free / hermetic: ``build_fleet_snapshot`` and ``render_snapshot`` are driven
through a CALL-RECORDING fake ``RegistryPort`` — a dict-backed double that
appends every method name it receives to a ``calls`` list and serves canned
``projects`` rows. Asserting that ``calls`` contains ONLY read methods after a
full build+render IS the OLB-04 read-only predicate (no live branch needed; the
seam is exercised against the actual shipped projection, not a parallel fake).
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from supervisor.ports import RegistryPort, RegistryRow
from supervisor.status_surface import (
    ACTIVE_RUN_NONE,
    ACTIVE_RUN_RUNNING,
    DEFAULT_CONCURRENCY_CEILING,
    build_fleet_snapshot,
    render_snapshot,
)

# The three RegistryPort write methods (Spec v1.3 §5.5). The write-nothing probe
# asserts none of these ever appears in the fake's recorded calls.
WRITE_METHODS = frozenset({"set_lifecycle_state", "record_run", "update_run_status"})

# A fixed build instant so the FR-061 freshness assertion is deterministic.
NOW = datetime(2026, 6, 5, 12, 30, 0, tzinfo=UTC)


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


# --- A call-recording fake RegistryPort: serves canned reads, records every call ---


class _RecordingRegistryPort:
    """Dict-backed :class:`RegistryPort` double.

    Serves the programmed candidate / running rows and appends the NAME of every
    method invoked to ``calls`` — so a test can assert that a build+render touched
    only the read methods (FR-058 / NFR-009 write-nothing probe). The write
    methods are present (structural conformance) but only record; they mutate
    nothing.
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


# Three Candidates (one carrying Attention Debt) + two running Projects (one
# carrying Attention Debt). Fleet rollup: candidate=3, running=2; total Attention
# Debt = 5 + 3 = 8; running_count = 2.
CANDIDATES: list[RegistryRow] = [
    _project_row("c1", "Candidate One", "candidate", 0),
    _project_row("c2", "Candidate Two", "candidate", 5),
    _project_row("c3", "Candidate Three", "candidate", 0),
]
RUNNING: list[RegistryRow] = [
    _project_row("r1", "Running One", "running", 3),
    _project_row("r2", "Running Two", "running", 0),
]


@pytest.fixture
def port() -> _RecordingRegistryPort:
    """A fresh call-recording fake over the canned fleet per test."""
    return _RecordingRegistryPort(CANDIDATES, RUNNING)


# --- structural conformance: the fake is a real RegistryPort ---


@pytest.mark.unit
def test_recording_fake_is_a_structural_registry_port(
    port: _RecordingRegistryPort,
) -> None:
    """The fake satisfies the OLB-02 RegistryPort seam (the surface depends only on
    this Protocol; ports.py untouched)."""
    assert isinstance(port, RegistryPort)


# --- (a) FR-059 consolidated row ---


@pytest.mark.unit
def test_build_returns_one_consolidated_row_per_project(
    port: _RecordingRegistryPort,
) -> None:
    """FR-059: N Projects in -> N consolidated rows out, each carrying
    lifecycle_state + active_run_status + attention_debt."""
    snapshot = build_fleet_snapshot(port, now=NOW)

    assert len(snapshot.rows) == len(CANDIDATES) + len(RUNNING)
    for row in snapshot.rows:
        assert row.lifecycle_state in {"candidate", "running"}
        assert row.active_run_status in {ACTIVE_RUN_RUNNING, ACTIVE_RUN_NONE}
        assert isinstance(row.attention_debt, int)


@pytest.mark.unit
def test_render_emits_one_line_per_project(port: _RecordingRegistryPort) -> None:
    """FR-059: render shows every Project's display name (one row per Project)."""
    rendered = render_snapshot(build_fleet_snapshot(port, now=NOW))

    for canned in CANDIDATES + RUNNING:
        assert str(canned["display_name"]) in rendered


# --- (b) FR-058 / NFR-009 write-nothing probe (the OLB-04 core predicate) ---


@pytest.mark.unit
def test_build_and_render_invoke_only_read_methods(
    port: _RecordingRegistryPort,
) -> None:
    """FR-058 / NFR-009: a full build+render calls ONLY read_candidates /
    read_running — none of the three write methods is ever invoked (no row
    mutation observed)."""
    render_snapshot(build_fleet_snapshot(port, now=NOW))

    assert set(port.calls) == {"read_candidates", "read_running"}
    assert WRITE_METHODS.isdisjoint(port.calls)


# --- (c) FR-061 freshness ---


@pytest.mark.unit
def test_render_carries_visible_as_of_timestamp(
    port: _RecordingRegistryPort,
) -> None:
    """FR-061: the rendered snapshot displays the ``now`` it was built at."""
    rendered = render_snapshot(build_fleet_snapshot(port, now=NOW))

    assert NOW.isoformat() in rendered
    assert "as of" in rendered


# --- (d) active-Run join ---


@pytest.mark.unit
def test_running_project_shows_active_run_candidate_shows_sentinel(
    port: _RecordingRegistryPort,
) -> None:
    """A Project with a running Run shows the active-Run status; a Candidate with
    no running Run shows the ``none`` sentinel."""
    snapshot = build_fleet_snapshot(port, now=NOW)
    by_id = {row.project_id: row for row in snapshot.rows}

    assert by_id["r1"].active_run_status == ACTIVE_RUN_RUNNING
    assert by_id["c1"].active_run_status == ACTIVE_RUN_NONE


# --- (e) fleet rollup ---


@pytest.mark.unit
def test_rollup_counts_debt_and_headroom_are_correct(
    port: _RecordingRegistryPort,
) -> None:
    """Counts-by-lifecycle-state, total Attention Debt, and Concurrency-Ceiling
    headroom (ceiling - running_count, ceiling=2) are correct for the fake fleet."""
    snapshot = build_fleet_snapshot(port, now=NOW)

    assert snapshot.counts_by_lifecycle_state == {"candidate": 3, "running": 2}
    assert snapshot.total_attention_debt == 8
    assert snapshot.running_count == 2
    assert snapshot.concurrency_ceiling == DEFAULT_CONCURRENCY_CEILING
    assert snapshot.headroom == DEFAULT_CONCURRENCY_CEILING - 2  # == 0


@pytest.mark.unit
def test_headroom_tracks_injected_ceiling(port: _RecordingRegistryPort) -> None:
    """The headroom denominator is the injected ceiling — the surface owns no
    ceiling (Spec §3; OLB-09 owns it)."""
    snapshot = build_fleet_snapshot(port, now=NOW, concurrency_ceiling=5)

    assert snapshot.concurrency_ceiling == 5
    assert snapshot.headroom == 5 - snapshot.running_count  # 5 - 2 == 3


@pytest.mark.unit
def test_empty_fleet_renders_a_clean_zero_row_snapshot() -> None:
    """A zero-Project registry yields an empty fleet: no rows, zero debt, full
    headroom — and still reads (never writes)."""
    empty = _RecordingRegistryPort([], [])

    snapshot = build_fleet_snapshot(empty, now=NOW)
    rendered = render_snapshot(snapshot)

    assert snapshot.rows == ()
    assert snapshot.total_attention_debt == 0
    assert snapshot.headroom == DEFAULT_CONCURRENCY_CEILING
    assert NOW.isoformat() in rendered
    assert WRITE_METHODS.isdisjoint(empty.calls)
