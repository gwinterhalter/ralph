"""Read-only Status Surface for the Outer Loop Supervisor (OLB-04).

A thin, in-process projection that assembles a per-Project consolidated fleet
view from the OLB-02 read seam (``RegistryPort.read_candidates`` /
``read_running``, ``supervisor/ports.py``) and renders it as a one-shot text
snapshot. Spec v1.3 §13 (the surface is a strict *consumer* — originates no
Dispatch, resolves no gate, writes nothing):

* FR-058 / NFR-009 read-only invariant: ``build_fleet_snapshot`` calls ONLY the
  port's read methods; it never invokes ``set_lifecycle_state`` / ``record_run``
  / ``update_run_status`` and mutates no substrate. ``render_snapshot`` returns a
  string (the caller prints) with no I/O side effect.
* FR-059 per-Project consolidated row: the thin subset resolved by gate
  ``olb04-status-surface-field-scope`` (option A) — ``lifecycle_state``,
  active-Run status (running Run via ``read_running``; a ``none`` sentinel when
  the Project has no running Run), and Attention Debt, plus the cheap fleet
  rollup (counts-by-lifecycle-state, total Attention Debt, Concurrency-Ceiling
  headroom). Per-Project open-work-count + cumulative cost + FR-062
  stale-heartbeat are forward-referenced to OLB-16.
* FR-060 no-shadow-state: every value is derived live from the rows read this
  call; the surface persists no copy that could diverge from the substrate.
* FR-061 freshness: the render carries a visible ``as of <as_of>`` timestamp.

§13.4 leaves rendering to the implementation and names a one-shot CLI snapshot
as an acceptable lighter-weight form; the auto-refreshing TUI is OLB-16.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from supervisor.ports import RegistryPort, RegistryRow

# Global Concurrency Ceiling (Spec v1.3 §3 / seed §2). Injected as a parameter so
# the surface sets no ceiling of its own — the authoritative owner is the OLB-09
# Cross-Project Scheduler; OLB-04 surfaces headroom against it only.
DEFAULT_CONCURRENCY_CEILING = 2

# Active-Run status sentinels. A Project surfaced by ``read_running`` has an active
# Run (``ACTIVE_RUN_RUNNING``); a Candidate surfaced by ``read_candidates`` has
# none (``ACTIVE_RUN_NONE``). These are the only run-state distinctions derivable
# through the OLB-02 read seam (gate ``olb04-status-surface-field-scope`` option A);
# the running/healthy-vs-stale FR-062 distinction is OLB-16.
ACTIVE_RUN_RUNNING = "running"
ACTIVE_RUN_NONE = "—"


@dataclass(frozen=True)
class ProjectStatusRow:
    """One per-Project consolidated row of the FR-059 thin subset (gate A).

    Every field is derived live from a single Registry row read this call
    (FR-060): ``lifecycle_state`` and ``attention_debt`` come straight off the
    ``projects`` row; ``active_run_status`` is the sentinel for which read set
    surfaced the Project (running vs candidate).
    """

    project_id: str
    display_name: str
    lifecycle_state: str
    active_run_status: str
    attention_debt: int


@dataclass(frozen=True)
class FleetSnapshot:
    """A consolidated fleet view built at a single instant (FR-059 + rollup).

    Holds the per-Project rows plus the cheap fleet rollup and the ``as_of``
    timestamp the snapshot was assembled at (FR-061). A pure value object — it
    persists no shadow state (FR-060); rebuild it to refresh.
    """

    rows: tuple[ProjectStatusRow, ...]
    counts_by_lifecycle_state: Mapping[str, int]
    total_attention_debt: int
    running_count: int
    concurrency_ceiling: int
    headroom: int
    as_of: datetime


def build_fleet_snapshot(
    port: RegistryPort,
    *,
    now: datetime,
    concurrency_ceiling: int = DEFAULT_CONCURRENCY_CEILING,
) -> FleetSnapshot:
    """Assemble a :class:`FleetSnapshot` from the OLB-02 read seam — a PURE projection.

    Reads Candidate and running Projects through the port's READ methods only
    (FR-058 / NFR-009 — no write call, no substrate mutation), joins them per
    Project (a running Project overrides a same-id Candidate, though the §5.2
    lifecycle states are disjoint), and derives every rendered value live from
    the rows read this call (FR-060). ``concurrency_ceiling`` is injected so the
    surface owns no ceiling (Spec §3; OLB-09 owns it).
    """
    rows_by_id: dict[str, ProjectStatusRow] = {}
    for row in port.read_candidates():
        candidate = _project_status_row(row, active_run_status=ACTIVE_RUN_NONE)
        rows_by_id[candidate.project_id] = candidate
    for row in port.read_running():
        running = _project_status_row(row, active_run_status=ACTIVE_RUN_RUNNING)
        rows_by_id[running.project_id] = running

    rows = tuple(sorted(rows_by_id.values(), key=lambda r: r.project_id))

    counts_by_lifecycle_state: dict[str, int] = {}
    for r in rows:
        counts_by_lifecycle_state[r.lifecycle_state] = (
            counts_by_lifecycle_state.get(r.lifecycle_state, 0) + 1
        )
    total_attention_debt = sum(r.attention_debt for r in rows)
    running_count = sum(
        1 for r in rows if r.active_run_status == ACTIVE_RUN_RUNNING
    )

    return FleetSnapshot(
        rows=rows,
        counts_by_lifecycle_state=counts_by_lifecycle_state,
        total_attention_debt=total_attention_debt,
        running_count=running_count,
        concurrency_ceiling=concurrency_ceiling,
        headroom=concurrency_ceiling - running_count,
        as_of=now,
    )


def render_snapshot(snapshot: FleetSnapshot) -> str:
    """Format a :class:`FleetSnapshot` as a one-shot text table (§13.4 snapshot form).

    Returns the rendered string — the caller prints it; this function performs no
    I/O on any substrate (FR-058 / NFR-009). The header carries a visible ``as
    of`` timestamp (FR-061); one line follows per Project, then the fleet rollup.
    """
    header = (
        f"{'PROJECT':<28} {'LIFECYCLE':<12} {'ACTIVE RUN':<12} {'ATTN DEBT':>9}"
    )
    rule = "-" * len(header)

    lines = [
        "Outer Loop Supervisor — Fleet Status",
        f"as of {snapshot.as_of.isoformat()}",
        "",
        header,
        rule,
    ]
    for row in snapshot.rows:
        lines.append(
            f"{row.display_name:<28} {row.lifecycle_state:<12} "
            f"{row.active_run_status:<12} {row.attention_debt:>9}"
        )
    lines.append(rule)

    counts = (
        ", ".join(
            f"{state}={count}"
            for state, count in sorted(snapshot.counts_by_lifecycle_state.items())
        )
        or "(none)"
    )
    lines.append(f"Projects: {len(snapshot.rows)}  |  Lifecycle: {counts}")
    lines.append(
        f"Total Attention Debt: {snapshot.total_attention_debt}  |  "
        f"Running: {snapshot.running_count}/{snapshot.concurrency_ceiling}  |  "
        f"Headroom: {snapshot.headroom}"
    )
    return "\n".join(lines)


def _project_status_row(
    row: RegistryRow, *, active_run_status: str
) -> ProjectStatusRow:
    """Project one Registry row onto the FR-059 thin subset (gate A).

    Reads only the columns the OLB-02 seam exposes (Spec §5.2): ``project_id``,
    ``display_name`` (falling back to ``project_id`` when blank), ``lifecycle_state``,
    and ``attention_debt``. No value is invented or persisted (FR-060).
    """
    project_id = str(row["project_id"])
    display_name_raw = row.get("display_name")
    display_name = str(display_name_raw) if display_name_raw else project_id
    return ProjectStatusRow(
        project_id=project_id,
        display_name=display_name,
        lifecycle_state=str(row["lifecycle_state"]),
        active_run_status=active_run_status,
        attention_debt=_coerce_attention_debt(row.get("attention_debt")),
    )


def _coerce_attention_debt(value: object) -> int:
    """Coerce the ``attention_debt`` column to a non-negative-display int.

    The column is an integer (Spec §5.2); a missing/``None``/non-int value reads
    as ``0`` rather than raising, so a partially-populated row still renders.
    ``bool`` is excluded (it is an ``int`` subclass but never a debt count).
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0
