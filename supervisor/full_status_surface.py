"""Full Status Surface for the Outer Loop Supervisor (OLB-16, Spec v1.3 §13).

The OLB-16 Full Status Surface SUPERSEDES the OLB-04 thin subset
(``supervisor/status_surface.py``): it renders the complete FR-059 per-Project
consolidated row plus the FR-062 stale-heartbeat distinction, FR-063 non-binding
cost framing, and FR-061 bounded auto-refresh — still as a strict read-only
*consumer* that originates no Dispatch, resolves no gate, and writes nothing
(FR-058 / NFR-009).

Resolved per gate ``olb16-status-surface-tui-form-and-scope`` (option A): this
module is the **pure compose/render core + a refresh scheduler** — every value is
golden-snapshot-assertable headlessly. The interactive terminal loop is a thin
``main()`` reader wrapper (:func:`run_status_loop` / the ``__main__`` block) that
is NOT under closure test (Spec §13 line 371 leaves the in-process-vs-separate-reader
form to the implementation).

The surface composes the closed OLB-04 thin projection read-only:
:func:`~supervisor.status_surface.build_fleet_snapshot` performs the FR-060
candidate/running join (each value derived live from the rows read this call — no
shadow state), and each thin row is then *enriched* with the two FR-059 fields
OLB-04 forward-referenced here:

* **open work-registry count** — the §13 FR-059 per-Project open-item count, and
* **cumulative cost** — the summed run cost, rendered as NON-BINDING operator
  information (FR-063; the enforcing authority is the OLB-12 Cost Circuit-Breaker,
  not this surface).

Neither field is a column on the OLB-02 ``projects`` read seam (``PROJECT_COLUMNS``),
so both — together with the FR-062 heartbeat instants — are supplied as injected
per-Project maps the live ``main()`` wrapper sources (from the work registry, the
``ralph_runs`` cost ledger, and the Heartbeat Pointer). The pure core reads the DB
for nothing, imports no driver, and reads no wall-clock (``now`` is injected),
so the whole module — and the unit suite over it — stays hermetic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Callable

from supervisor.status_surface import (
    ACTIVE_RUN_RUNNING,
    DEFAULT_CONCURRENCY_CEILING,
    build_fleet_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from supervisor.ports import RegistryPort
    from supervisor.status_surface import ProjectStatusRow

# --- FR-061 bounded refresh ---------------------------------------------------
# The default bounded auto-refresh interval (Spec v1.3 §13 FR-061). The surface
# refreshes no faster than this; the live loop sleeps it between rebuilds. Injected
# as a parameter so a caller may widen it — the surface owns no timer of its own.
DEFAULT_REFRESH_INTERVAL = timedelta(seconds=5)

# --- FR-062 stale-heartbeat threshold -----------------------------------------
# The default age past which a running Project's last heartbeat reads as STALE
# (Spec v1.3 §13 FR-062). A running Run whose most-recent heartbeat is older than
# this — or that has emitted none at all — is marked visibly stalled, distinct from
# a healthy Run. Injected so the threshold is the operator's, not the surface's.
DEFAULT_HEARTBEAT_STALE_AFTER = timedelta(minutes=15)

# --- FR-062 heartbeat-state sentinels -----------------------------------------
# Distinct, human-legible markers so a stalled Run is visibly NOT a healthy one.
HEARTBEAT_HEALTHY = "healthy"
HEARTBEAT_STALLED = "STALLED"
# Heartbeat is only meaningful for a running Run; a Candidate / non-running Project
# has none to assess and reads as not-applicable (never spuriously "healthy").
HEARTBEAT_NA = "—"

# --- FR-063 non-binding cost framing ------------------------------------------
# The cost column header + footnote frame the cumulative cost as operator
# information ONLY — this surface enforces nothing on it (the §10 Cost
# Circuit-Breaker is the enforcing authority). Named once so the render and the
# golden-snapshot test reference the same string.
COST_COLUMN_LABEL = "COST(info)"
COST_NON_BINDING_NOTE = (
    "Cost is operator information only — NON-BINDING. The Cost Circuit-Breaker "
    "(Spec §10) is the enforcing authority; this surface writes nothing (FR-063)."
)

# Money grain for display totals — the persisted ``ralph_runs.terminal_cost_usd``
# numeric(10,4) scale (mirrors run_lifecycle.TERMINAL_COST_SCALE so a rendered
# total reads at the same grain the substrate stores). No float anywhere (NFR-007).
_COST_SCALE = Decimal("0.0001")
_ZERO_COST = Decimal("0").quantize(_COST_SCALE)


@dataclass(frozen=True)
class ProjectFullStatusRow:
    """One per-Project consolidated row of the FULL FR-059 five-field surface.

    Extends the OLB-04 thin subset (``lifecycle_state`` + ``active_run_status`` +
    ``attention_debt``) with the two fields OLB-04 forward-referenced to OLB-16 —
    ``open_work_count`` and ``cumulative_cost_usd`` — plus the FR-062 derived
    ``heartbeat_state`` (and the ``heartbeat_as_of`` instant it was judged from).
    Every value is derived live from the rows + injected maps read this call
    (FR-060: no value is persisted where it could diverge from the substrate).
    """

    project_id: str
    display_name: str
    lifecycle_state: str
    active_run_status: str
    attention_debt: int
    open_work_count: int
    cumulative_cost_usd: Decimal
    heartbeat_state: str
    heartbeat_as_of: datetime | None


@dataclass(frozen=True)
class FullFleetSnapshot:
    """A consolidated FULL fleet view built at a single instant (FR-059 + rollup).

    Holds the full per-Project rows plus the fleet rollup (counts-by-lifecycle,
    total Attention Debt, total open work, total cumulative cost, running/stalled
    counts, Concurrency-Ceiling headroom), the ``as_of`` instant the snapshot was
    assembled at, and the bounded ``refresh_interval`` the live loop honours
    (FR-061). A pure value object — it persists no shadow state (FR-060); rebuild
    it to refresh.
    """

    rows: tuple[ProjectFullStatusRow, ...]
    counts_by_lifecycle_state: Mapping[str, int]
    total_attention_debt: int
    total_open_work_count: int
    total_cumulative_cost_usd: Decimal
    running_count: int
    stalled_count: int
    concurrency_ceiling: int
    headroom: int
    as_of: datetime
    refresh_interval: timedelta


def build_full_fleet_snapshot(
    port: RegistryPort,
    *,
    now: datetime,
    open_work_counts: Mapping[str, int] | None = None,
    cumulative_costs: Mapping[str, Decimal] | None = None,
    heartbeats: Mapping[str, datetime] | None = None,
    concurrency_ceiling: int = DEFAULT_CONCURRENCY_CEILING,
    heartbeat_stale_after: timedelta = DEFAULT_HEARTBEAT_STALE_AFTER,
    refresh_interval: timedelta = DEFAULT_REFRESH_INTERVAL,
) -> FullFleetSnapshot:
    """Assemble a :class:`FullFleetSnapshot` — a PURE read-only projection (FR-058 / NFR-009).

    Composes the closed OLB-04 thin projection
    (:func:`~supervisor.status_surface.build_fleet_snapshot`, which reads the OLB-02
    seam's READ methods only — no write call, no substrate mutation) for the FR-060
    candidate/running join + Attention Debt, then enriches each thin row with the two
    FR-059 fields OLB-04 deferred here (open work-registry count + cumulative cost)
    and the FR-062 heartbeat-state judged against ``now`` and ``heartbeat_stale_after``.

    ``open_work_counts`` / ``cumulative_costs`` / ``heartbeats`` are injected
    per-Project maps (the live wiring sources them from the work registry, the
    ``ralph_runs`` cost ledger, and the Heartbeat Pointer; missing entries read as
    0 / ``Decimal('0')`` / no-heartbeat). The surface owns no ceiling or clock
    (Spec §3; both injected). Writes nothing.
    """
    open_work_counts = open_work_counts or {}
    cumulative_costs = cumulative_costs or {}
    heartbeats = heartbeats or {}

    thin = build_fleet_snapshot(port, now=now, concurrency_ceiling=concurrency_ceiling)

    rows = tuple(
        _enrich_row(
            thin_row,
            now=now,
            open_work_counts=open_work_counts,
            cumulative_costs=cumulative_costs,
            heartbeats=heartbeats,
            heartbeat_stale_after=heartbeat_stale_after,
        )
        for thin_row in thin.rows
    )

    total_open_work_count = sum(r.open_work_count for r in rows)
    total_cumulative_cost_usd = (
        sum((r.cumulative_cost_usd for r in rows), _ZERO_COST)
    ).quantize(_COST_SCALE)
    stalled_count = sum(1 for r in rows if r.heartbeat_state == HEARTBEAT_STALLED)

    return FullFleetSnapshot(
        rows=rows,
        counts_by_lifecycle_state=thin.counts_by_lifecycle_state,
        total_attention_debt=thin.total_attention_debt,
        total_open_work_count=total_open_work_count,
        total_cumulative_cost_usd=total_cumulative_cost_usd,
        running_count=thin.running_count,
        stalled_count=stalled_count,
        concurrency_ceiling=thin.concurrency_ceiling,
        headroom=thin.headroom,
        as_of=now,
        refresh_interval=refresh_interval,
    )


def _enrich_row(
    thin_row: ProjectStatusRow,
    *,
    now: datetime,
    open_work_counts: Mapping[str, int],
    cumulative_costs: Mapping[str, Decimal],
    heartbeats: Mapping[str, datetime],
    heartbeat_stale_after: timedelta,
) -> ProjectFullStatusRow:
    """Project one OLB-04 thin row onto the full FR-059 five-field row.

    Reuses the thin row's already-joined ``lifecycle_state`` / ``active_run_status``
    / ``attention_debt`` (FR-060 — derived live, never re-read) and adds the two
    deferred FR-059 fields + the FR-062 heartbeat-state. No value is invented: a
    Project unlisted in a supplied map reads its zero default.
    """
    project_id = thin_row.project_id
    heartbeat_as_of = heartbeats.get(project_id)
    return ProjectFullStatusRow(
        project_id=project_id,
        display_name=thin_row.display_name,
        lifecycle_state=thin_row.lifecycle_state,
        active_run_status=thin_row.active_run_status,
        attention_debt=thin_row.attention_debt,
        open_work_count=_coerce_open_work(open_work_counts.get(project_id)),
        cumulative_cost_usd=_coerce_cost(cumulative_costs.get(project_id)),
        heartbeat_state=_heartbeat_state(
            active_run_status=thin_row.active_run_status,
            heartbeat_as_of=heartbeat_as_of,
            now=now,
            stale_after=heartbeat_stale_after,
        ),
        heartbeat_as_of=heartbeat_as_of,
    )


def _heartbeat_state(
    *,
    active_run_status: str,
    heartbeat_as_of: datetime | None,
    now: datetime,
    stale_after: timedelta,
) -> str:
    """Classify a Project's heartbeat freshness (Spec v1.3 §13 FR-062).

    Heartbeat is only meaningful for a Project with a running Run: a non-running
    Project reads :data:`HEARTBEAT_NA` (never a spurious "healthy"). A running Run
    with NO observed heartbeat, or whose most-recent heartbeat is older than
    ``stale_after``, reads :data:`HEARTBEAT_STALLED` — visibly distinct from a
    :data:`HEARTBEAT_HEALTHY` Run whose heartbeat is within the window.
    """
    if active_run_status != ACTIVE_RUN_RUNNING:
        return HEARTBEAT_NA
    if heartbeat_as_of is None:
        return HEARTBEAT_STALLED
    if now - heartbeat_as_of > stale_after:
        return HEARTBEAT_STALLED
    return HEARTBEAT_HEALTHY


def _coerce_open_work(value: object) -> int:
    """Coerce an injected open-work count to a non-negative-display int.

    A missing / ``None`` / non-int value reads as ``0`` rather than raising, so a
    partially-populated fleet still renders. ``bool`` is excluded (an ``int``
    subclass that is never a work count).
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _coerce_cost(value: object) -> Decimal:
    """Coerce an injected cumulative cost to an exact ``Decimal`` at the money grain.

    Money is exact (NFR-007 — no float). A missing / ``None`` / ``bool`` value reads as
    ``Decimal('0')`` (``bool`` is excluded as a non-cost, symmetric with
    :func:`_coerce_open_work`, so a stray flag never becomes ``Decimal('True')`` and
    crashes the build — the surface renders a partially-populated fleet without raising);
    a finite non-``Decimal`` value is taken via ``str`` so a stray float never seeds
    binary rounding. Quantized to the persisted numeric(10,4) grain.
    """
    if value is None or isinstance(value, bool):
        return _ZERO_COST
    if isinstance(value, Decimal):
        return value.quantize(_COST_SCALE) if value.is_finite() else _ZERO_COST
    return Decimal(str(value)).quantize(_COST_SCALE)


def render_full_snapshot(snapshot: FullFleetSnapshot) -> str:
    """Format a :class:`FullFleetSnapshot` as a full text table — performs NO I/O.

    Returns the rendered string (the caller prints); this function mutates no
    substrate (FR-058 / NFR-009). The header carries a visible ``as of`` timestamp
    and the bounded ``refresh every`` interval (FR-061); one line follows per
    Project showing the full FR-059 five-field row + the FR-062 heartbeat marker;
    then the fleet rollup; then the FR-063 non-binding-cost footnote.
    """
    header = (
        f"{'PROJECT':<24} {'LIFECYCLE':<12} {'ACTIVE RUN':<10} "
        f"{'ATTN':>5} {'OPEN':>5} {COST_COLUMN_LABEL:>12} {'HEARTBEAT':>9}"
    )
    rule = "-" * len(header)

    lines = [
        "Outer Loop Supervisor — Full Fleet Status",
        f"as of {snapshot.as_of.isoformat()} | "
        f"refresh every {_format_interval(snapshot.refresh_interval)}",
        "",
        header,
        rule,
    ]
    for row in snapshot.rows:
        lines.append(
            f"{row.display_name:<24} {row.lifecycle_state:<12} "
            f"{row.active_run_status:<10} {row.attention_debt:>5} "
            f"{row.open_work_count:>5} {_format_cost(row.cumulative_cost_usd):>12} "
            f"{row.heartbeat_state:>9}"
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
        f"Open Work: {snapshot.total_open_work_count}  |  "
        f"Total Cost: {_format_cost(snapshot.total_cumulative_cost_usd)}"
    )
    lines.append(
        f"Running: {snapshot.running_count}/{snapshot.concurrency_ceiling}  |  "
        f"Headroom: {snapshot.headroom}  |  "
        f"Stalled heartbeats: {snapshot.stalled_count}"
    )
    lines.append("")
    lines.append(COST_NON_BINDING_NOTE)
    return "\n".join(lines)


def _format_cost(cost: Decimal) -> str:
    """Render a money value at the display grain with a ``$`` prefix (NFR-007)."""
    return f"${cost.quantize(_COST_SCALE)}"


def _format_interval(interval: timedelta) -> str:
    """Render a refresh interval as a compact whole/fractional-seconds string."""
    seconds = interval.total_seconds()
    if seconds == int(seconds):
        return f"{int(seconds)}s"
    return f"{seconds}s"


# --- FR-061 refresh scheduler (the bounded-cadence core, gate A) --------------


class RefreshScheduler:
    """The FR-061 bounded refresh cadence — a PURE, clock-injected scheduler.

    Owns the bounded ``interval`` and answers, for a given last-built ``as_of`` and
    the current ``now``, whether a rebuild is due and when the next one falls. The
    surface refreshes no faster than ``interval`` (the bound). Holds no wall-clock
    and no timer of its own — ``now`` is always supplied — so it is deterministic and
    golden-assertable; the live loop (:func:`run_status_loop`) is the only place a
    real clock and sleep appear.
    """

    def __init__(self, *, interval: timedelta = DEFAULT_REFRESH_INTERVAL) -> None:
        if interval <= timedelta(0):
            raise ValueError(f"refresh interval must be positive, got {interval!r}")
        self._interval = interval

    @property
    def interval(self) -> timedelta:
        """The bounded refresh interval (the surface refreshes no faster)."""
        return self._interval

    def next_refresh_at(self, last_as_of: datetime) -> datetime:
        """The instant the next refresh is due after a snapshot built at ``last_as_of``."""
        return last_as_of + self._interval

    def is_due(self, *, last_as_of: datetime, now: datetime) -> bool:
        """True iff ``now`` has reached the next bounded refresh after ``last_as_of``."""
        return now >= self.next_refresh_at(last_as_of)

    def seconds_until_due(self, *, last_as_of: datetime, now: datetime) -> float:
        """Non-negative seconds until the next refresh is due (``0.0`` if already due)."""
        remaining = (self.next_refresh_at(last_as_of) - now).total_seconds()
        return max(0.0, remaining)


# --- Thin interactive reader wrapper (NOT under closure test, gate A) ---------
# Per gate olb16-status-surface-tui-form-and-scope = A, the interactive terminal
# loop is delivered but is NOT part of the headless closure test: it reads a real
# clock and sleeps. The pure core above (build / render / RefreshScheduler) carries
# every FR the unit suite asserts; this wrapper only drives them on a cadence.


def run_status_loop(
    fetch_snapshot: Callable[[], FullFleetSnapshot],
    *,
    scheduler: RefreshScheduler,
    emit: Callable[[str], None],
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
    max_refreshes: int | None = None,
) -> None:  # pragma: no cover - thin reader wrapper, not under closure test (gate A)
    """Drive the surface on the FR-061 bounded cadence — the thin reader loop.

    Rebuilds via ``fetch_snapshot`` (which assembles a :class:`FullFleetSnapshot`
    from the live substrate), renders, ``emit``s the text, then sleeps until the
    scheduler says the next refresh is due. ``max_refreshes`` bounds the loop for a
    one-shot / smoke invocation (``None`` runs until interrupted). Every effectful
    collaborator — the clock, the sleep, the output sink, the fetch — is injected,
    so this wrapper holds no policy of its own; it is the un-tested front-end the
    pure core serves (gate A).
    """
    refreshes = 0
    while max_refreshes is None or refreshes < max_refreshes:
        snapshot = fetch_snapshot()
        emit(render_full_snapshot(snapshot))
        refreshes += 1
        # Sleep only when another refresh will follow — the while-condition owns the
        # termination bound; this just skips the trailing idle wait on the final pass.
        if max_refreshes is None or refreshes < max_refreshes:
            sleep(scheduler.seconds_until_due(last_as_of=snapshot.as_of, now=now()))


__all__ = [
    "DEFAULT_REFRESH_INTERVAL",
    "DEFAULT_HEARTBEAT_STALE_AFTER",
    "HEARTBEAT_HEALTHY",
    "HEARTBEAT_STALLED",
    "HEARTBEAT_NA",
    "COST_COLUMN_LABEL",
    "COST_NON_BINDING_NOTE",
    "ProjectFullStatusRow",
    "FullFleetSnapshot",
    "build_full_fleet_snapshot",
    "render_full_snapshot",
    "RefreshScheduler",
    "run_status_loop",
]
