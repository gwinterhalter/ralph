"""C5 final system pass — the terminal integration checkpoint (OLB-16).

The capstone of the C1–C5 series. Exercises the supervisor's FR-036 fleet-wide
Kill-Switch drill — through the additively-wired
:func:`~supervisor.cycle_wiring.run_kill_switch_halt` (gate
``olb16-c5-killswitch-fleet-halt-wiring-scope`` = A) — against disposable
``oltest_c5_*`` Projects on the disposable Supabase dev branch (ref
``jmjncijbbakuzndqhssw``), asserting the OLB-16 predicate facets against the ACTUAL
branch ``projects`` / ``ralph_runs`` rows (Spec v1.3 §9 FR-036 / FR-038, §13):

  Kill-Switch fleet-halt drill (§9.2 FR-036):
    * a multi-``running`` fleet with the global Kill-Switch ENGAGED → every running Run
      is signalled to a safe stop (each Project tripped to ``paused_safety`` + a
      top-tier escalation), and
    * no further Dispatch is issued fleet-wide — a Candidate that would otherwise spawn
      is REFUSED at the OLB-06 §9.3 hard floor (the spawn port is never invoked).

  No-silent-kill capstone (§9.2 FR-038):
    * across the halt, every trip resolves to ``paused_safety`` — no ``oltest_c5`` Run
      row is ever moved off ``running`` to a terminal / killed status, and no Project row
      is moved to ``failed``.

  Full Status Surface over the live fleet (§13 FR-058–063):
    * the OLB-16 full surface renders the live fleet read-only (FR-058 / NFR-009 — the
      live ``lifecycle_state`` rows are unchanged by a build+render).

Substrate (gate ``olb16-c5-system-pass-substrate`` = A): the drill runs against REAL
branch rows + the real OLB-02 ``Registry`` (psycopg), with the spawn STUBBED (no
``orchestrator.sh`` is launched and no ``ralph_runs`` Run is ever drained) — the C4 /
iter-0028 pattern. The C5 predicate verifies the fleet-halt RESPONSE logic, observable
against real branch rows at near-zero LLM spend; harness completion (C2/OLB-08) and
scheduling throughput (C3/OLB-11) were proven LIVE-green at iter-0021 / iter-0025, and
the anomaly drills (C4/OLB-14) at iter-0028.

Live boundary: collected on every run but SKIPPED unless ``OL_SUPERVISOR_DB_URL`` is set
AND points at the disposable branch (the production ref must be ABSENT) — a structural
guarantee this checkpoint can never touch production ``eybdbshxswutgaaylpol``.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import cast

import psycopg
import pytest

from supervisor.admission import (
    AdmissionRejection,
    SeedFinding,
    SpawnResult,
)
from supervisor.attention import UrgencyTier, assign_urgency_tier
from supervisor.cycle_wiring import (
    AttentionStateStore,
    KillSwitchConfig,
    ScheduleConfig,
    run_kill_switch_halt,
    run_schedule_step,
)
from supervisor.full_status_surface import (
    COST_NON_BINDING_NOTE,
    HEARTBEAT_STALLED,
    build_full_fleet_snapshot,
    render_full_snapshot,
)
from supervisor.ports import RegistryRow
from supervisor.registry import DBConnection, Registry
from supervisor.safety_gates import KILL_SWITCH_ENGAGED, KillSwitch

# --- Live branch gating (structural production-safety guard) ------------------
DB_URL_ENV = "OL_SUPERVISOR_DB_URL"
BRANCH_REF = "jmjncijbbakuzndqhssw"
PRODUCTION_REF = "eybdbshxswutgaaylpol"

_DSN = os.environ.get(DB_URL_ENV, "")
# Run ONLY against the disposable branch: its ref must be present AND the production
# ref must be absent. If the DSN ever resolves to production, the checkpoint skips —
# it is structurally incapable of writing production rows.
_ON_BRANCH = bool(_DSN) and BRANCH_REF in _DSN and PRODUCTION_REF not in _DSN

requires_branch = pytest.mark.skipif(
    not _ON_BRANCH,
    reason=(
        f"C5 live final-system-pass checkpoint requires {DB_URL_ENV} pointing at the "
        f"disposable branch {BRANCH_REF} (production ref {PRODUCTION_REF} must be absent)."
    ),
)

pytestmark = [pytest.mark.integration, requires_branch]

# --- Disposable oltest_c5 Projects -------------------------------------------
PROJECT_A = "oltest_c5_run_a"  # running -> signalled to a safe stop
PROJECT_B = "oltest_c5_run_b"  # running -> signalled to a safe stop
PROJECT_C = "oltest_c5_run_c"  # running -> signalled to a safe stop
PROJECT_CAND = "oltest_c5_candidate"  # candidate -> Dispatch refused under the halt

_RUNNING_FLEET = (PROJECT_A, PROJECT_B, PROJECT_C)

# The §9.2 FR-034 read-only corpus path every Blast-Radius Scope must list (so the
# candidate clears the read-only invariant and reaches the Kill-Switch floor check).
READ_ONLY_CORPUS = "Project_Docs_Current\\"

_NOW = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)


# --- Branch substrate provisioning + restore (row-state only; no reshape) ------


def _connect(*, autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(_DSN)
    if autocommit:
        conn.autocommit = True
    return conn


def _registry(conn: psycopg.Connection) -> Registry:
    """Build the live OLB-02 Registry over a branch connection (the C3/C4 cast bridge)."""
    return Registry(cast(DBConnection, conn))


def _provision_running(conn: psycopg.Connection, project_ids: Sequence[str]) -> None:
    """Seed the named disposable oltest_c5 Projects ``running`` with one active Run each.

    Clears any prior oltest_c5 rows first, then inserts a ``running`` ``projects`` row +
    a ``running`` ``ralph_runs`` row per id, so the halt reads them as the running fleet
    and the no-silent-kill capstone can prove the Run row is never killed by a trip."""
    cur = conn.cursor()
    cur.execute("DELETE FROM ralph_runs WHERE project_slug LIKE 'oltest_c5%%'")
    cur.execute("DELETE FROM projects WHERE project_id LIKE 'oltest_c5%%'")
    for project_id in project_ids:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, status, lifecycle_state) "
            "VALUES (%s, %s, 'active', 'running')",
            (project_id, project_id),
        )
        cur.execute(
            "INSERT INTO ralph_runs (project_slug, seed_path, status) "
            "VALUES (%s, %s, 'running')",
            (project_id, f"oltest_c5://{project_id}/seed.md"),
        )
    conn.commit()


def _provision_candidate(conn: psycopg.Connection, project_id: str) -> None:
    """Seed one disposable oltest_c5 Project ``candidate`` (no Run), for the no-dispatch
    facet. Clears prior oltest_c5 rows first (row-state only; no table reshape)."""
    cur = conn.cursor()
    cur.execute("DELETE FROM ralph_runs WHERE project_slug LIKE 'oltest_c5%%'")
    cur.execute("DELETE FROM projects WHERE project_id LIKE 'oltest_c5%%'")
    cur.execute(
        "INSERT INTO projects (project_id, display_name, status, lifecycle_state) "
        "VALUES (%s, %s, 'active', 'candidate')",
        (project_id, project_id),
    )
    conn.commit()


def _restore(conn: psycopg.Connection) -> None:
    """Restore the pre-run branch state — the oltest_c5 rows were absent before this
    checkpoint, so deleting them (row-state only; no table reshape, Spec §4.2) is the
    exact restore."""
    cur = conn.cursor()
    cur.execute("DELETE FROM ralph_runs WHERE project_slug LIKE 'oltest_c5%%'")
    cur.execute("DELETE FROM projects WHERE project_id LIKE 'oltest_c5%%'")
    conn.commit()


def _lifecycle(conn: psycopg.Connection, project_id: str) -> str:
    cur = conn.cursor()
    cur.execute(
        "SELECT lifecycle_state FROM projects WHERE project_id = %s", (project_id,)
    )
    row = cur.fetchone()
    assert row is not None, f"no projects row for {project_id!r}"
    return str(row[0])


def _run_status(conn: psycopg.Connection, project_id: str) -> str:
    cur = conn.cursor()
    cur.execute(
        "SELECT status FROM ralph_runs WHERE project_slug = %s", (project_id,)
    )
    row = cur.fetchone()
    assert row is not None, f"no ralph_runs row for {project_id!r}"
    return str(row[0])


def _is_fleet_member(row: RegistryRow) -> bool:
    """Scope the halt / schedule to its own disposable oltest_c5 Projects only.

    The live branch carries other initiatives' Projects (e.g. ``ol_build`` / ``rl_test`` /
    ``oltest_c3`` / ``oltest_c4``); this predicate isolates the read to the checkpoint's
    own rows so no other initiative's live Run is ever signalled to stop or refused."""
    return str(row["project_id"]).startswith("oltest_c5_")


# --- Fakes for the no-further-dispatch facet (the spawn must NEVER be invoked) --


class _CleanSeedValidator:
    """A :class:`~supervisor.admission.SeedValidatorPort` that finds no SEVERE issue —
    so the candidate clears FR-016 and the gate reaches the §9.3 Kill-Switch floor."""

    def validate_seed(self, candidate: RegistryRow) -> Sequence[SeedFinding]:
        return ()


class _NeverSpawnPort:
    """A :class:`~supervisor.admission.SpawnPort` that records invocations and FAILS the
    test if ever called — the FR-036 proof that an engaged Kill-Switch issues NO Dispatch."""

    def __init__(self) -> None:
        self.invoked = False

    def spawn(self, seed_path: str, blast_radius_scope: object) -> SpawnResult:
        self.invoked = True
        raise AssertionError(
            "spawn invoked while the Kill-Switch was engaged — FR-036 violated "
            "(no Dispatch may be issued fleet-wide under the halt)"
        )


def _enriched_candidate(discovered: RegistryRow) -> RegistryRow:
    """The discovered branch row + the seed-derived §6 admission inputs.

    ``read_candidates`` surfaces only the ``projects`` columns, so the fields the
    Admission Gate reads to clear FR-016–020 (and reach the §9.3 Kill-Switch floor) are
    merged in here. Test orchestration over the public seam — no closed component edited."""
    row = dict(discovered)
    row.update(
        {
            "seed_path": f"oltest_c5://{discovered['project_id']}/seed.md",
            "initiative_slug": str(discovered["project_id"]),
            "open_item_count": 1,
            "writable_paths": [f"oltest_c5://{discovered['project_id']}/work"],
            "mcp_roots": ["oltest_c5://mcp"],
            "read_only_paths": [READ_ONLY_CORPUS],
        }
    )
    return row


# --- Kill-Switch fleet-halt drill: §9.2 FR-036 -------------------------------


@pytest.mark.integration
def test_c5_killswitch_halt_signals_every_running_run_to_a_safe_stop() -> None:
    """OLB-16 fleet-halt facet (FR-036): with the global Kill-Switch ENGAGED, every
    ``running`` Project in the fleet is signalled to a safe stop — tripped to
    ``paused_safety`` + a top-tier escalation — while every Run row is left ``running``
    (NOT killed, FR-038). Asserted against actual branch rows."""
    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision_running(verify, _RUNNING_FLEET)
        registry = _registry(live)
        store = AttentionStateStore()
        config = KillSwitchConfig(
            kill_switch=KillSwitch(engaged=True),
            attention_store=store,
            clock=lambda: _NOW,
            project_filter=_is_fleet_member,
        )

        outcome = run_kill_switch_halt(registry, config)

        # FR-036: dispatch halted; every running Project signalled to a safe stop.
        assert outcome.engaged is True
        assert outcome.dispatch_allowed is False
        assert set(outcome.stopped_projects) == set(_RUNNING_FLEET)
        for project_id in _RUNNING_FLEET:
            assert _lifecycle(verify, project_id) == "paused_safety"
            assert _run_status(verify, project_id) == "running"  # no kill (FR-038)
        # Every safe-stop raised a top-tier escalation (FR-029); Attention Debt accrued.
        assert len(outcome.escalations) == len(_RUNNING_FLEET)
        assert all(assign_urgency_tier(e) is UrgencyTier.TOP for e in outcome.escalations)
        for project_id in _RUNNING_FLEET:
            assert store.load().debt_for(project_id) == 1
    finally:
        _restore(verify)
        live.close()
        verify.close()


@pytest.mark.integration
def test_c5_killswitch_disengaged_is_a_noop() -> None:
    """A disengaged Kill-Switch halts nothing — the running fleet keeps running and
    Dispatch stays allowed (the halt is engaged-only)."""
    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision_running(verify, _RUNNING_FLEET)
        registry = _registry(live)
        config = KillSwitchConfig(
            kill_switch=KillSwitch(engaged=False),
            clock=lambda: _NOW,
            project_filter=_is_fleet_member,
        )

        outcome = run_kill_switch_halt(registry, config)

        assert outcome.engaged is False
        assert outcome.dispatch_allowed is True
        assert outcome.stopped_projects == ()
        for project_id in _RUNNING_FLEET:
            assert _lifecycle(verify, project_id) == "running"  # untouched
    finally:
        _restore(verify)
        live.close()
        verify.close()


# --- No-further-dispatch facet: §9.3 precedence (FR-036) ----------------------


@pytest.mark.integration
def test_c5_killswitch_refuses_all_further_dispatch() -> None:
    """OLB-16 no-dispatch facet (FR-036 / §9.3): with the Kill-Switch ENGAGED, a
    Candidate that would otherwise spawn is REFUSED at the OLB-06 hard floor — the spawn
    port is never invoked and the Candidate stays ``candidate`` (no Run row written)."""
    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision_candidate(verify, PROJECT_CAND)
        registry = _registry(live)
        spawn_port = _NeverSpawnPort()
        config = ScheduleConfig(
            seed_validator=_CleanSeedValidator(),
            spawn_port=spawn_port,
            kill_switch=KillSwitch(engaged=True),
            candidate_enricher=_enriched_candidate,
            project_filter=_is_fleet_member,
        )

        decision = run_schedule_step(registry, config)

        # The scheduler still SELECTS the candidate for a spawn (the §9.3 hard floor is
        # admission's, not the scheduler's) — but admission REFUSES it under the halt.
        assert decision is not None
        assert decision.project_id == PROJECT_CAND
        # FR-036: no spawn was issued — the port was never called, and the Candidate was
        # never moved to `running` (no Run row exists for it).
        assert spawn_port.invoked is False
        assert _lifecycle(verify, PROJECT_CAND) == "candidate"
        cur = verify.cursor()
        cur.execute(
            "SELECT count(*) FROM ralph_runs WHERE project_slug = %s", (PROJECT_CAND,)
        )
        assert cur.fetchone()[0] == 0
    finally:
        _restore(verify)
        live.close()
        verify.close()


@pytest.mark.integration
def test_c5_killswitch_refusal_reason_is_passed_through() -> None:
    """The §9.3 safety refusal under the halt is the canonical KILL_SWITCH_ENGAGED reason
    — admission passes the OLB-06 floor refusal through, never inventing a new code."""
    from supervisor.admission import admit_candidate

    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision_candidate(verify, PROJECT_CAND)
        registry = _registry(live)
        candidate = _enriched_candidate(
            {"project_id": PROJECT_CAND, "display_name": PROJECT_CAND}
        )

        result = admit_candidate(
            candidate,
            seed_validator=_CleanSeedValidator(),
            registry_port=registry,
            spawn_port=_NeverSpawnPort(),
            kill_switch=KillSwitch(engaged=True),
            running_count=0,
        )

        assert isinstance(result, AdmissionRejection)
        assert result.reason == KILL_SWITCH_ENGAGED
        assert _lifecycle(verify, PROJECT_CAND) == "candidate"  # never admitted
    finally:
        _restore(verify)
        live.close()
        verify.close()


# --- No-silent-kill capstone: §9.2 FR-038 ------------------------------------


@pytest.mark.integration
def test_c5_halt_kills_no_run_row() -> None:
    """OLB-16 no-silent-kill capstone (FR-038): across the fleet-wide halt, every trip
    resolves to ``paused_safety`` — NOT ONE ``oltest_c5`` Run row is moved off
    ``running`` to a terminal / killed status, and no tripped Project is ``failed``."""
    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision_running(verify, _RUNNING_FLEET)
        registry = _registry(live)
        config = KillSwitchConfig(
            kill_switch=KillSwitch(engaged=True),
            attention_store=AttentionStateStore(),
            clock=lambda: _NOW,
            project_filter=_is_fleet_member,
        )

        run_kill_switch_halt(registry, config)

        # FR-038: every Run row is still `running` — no kill, no terminal status anywhere.
        cur = verify.cursor()
        cur.execute(
            "SELECT project_slug, status FROM ralph_runs "
            "WHERE project_slug LIKE 'oltest_c5%%'"
        )
        run_rows = cur.fetchall()
        assert len(run_rows) == len(_RUNNING_FLEET)
        assert all(str(status) == "running" for _slug, status in run_rows)
        # Every halted Project is `paused_safety` (never `failed`).
        for project_id in _RUNNING_FLEET:
            assert _lifecycle(verify, project_id) == "paused_safety"
    finally:
        _restore(verify)
        live.close()
        verify.close()


# --- Full Status Surface over the live fleet: §13 FR-058–063 ------------------


@pytest.mark.integration
def test_c5_full_status_surface_renders_over_live_fleet_read_only() -> None:
    """OLB-16 surface facet (FR-058–063 / NFR-009): the full surface renders the LIVE
    fleet read-only — a build+render over real branch rows surfaces every oltest_c5
    Project (with injected open-work / cost / heartbeat supplements), frames cost as
    non-binding, marks a stale heartbeat — and changes NO ``lifecycle_state`` row."""
    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision_running(verify, _RUNNING_FLEET)
        registry = _registry(live)

        before = {p: _lifecycle(verify, p) for p in _RUNNING_FLEET}

        # Injected supplemental maps (the live wiring's source; here constructed): C lags
        # 30 minutes on its heartbeat (> the 15m default → STALLED).
        open_work_counts = {PROJECT_A: 4, PROJECT_B: 2, PROJECT_C: 9}
        cumulative_costs = {
            PROJECT_A: Decimal("1.5000"),
            PROJECT_B: Decimal("0.2500"),
            PROJECT_C: Decimal("8.0000"),
        }
        heartbeats = {
            PROJECT_A: _NOW - timedelta(minutes=1),
            PROJECT_B: _NOW - timedelta(minutes=2),
            PROJECT_C: _NOW - timedelta(minutes=30),
        }

        snapshot = build_full_fleet_snapshot(
            registry,
            now=_NOW,
            open_work_counts=open_work_counts,
            cumulative_costs=cumulative_costs,
            heartbeats=heartbeats,
        )
        rendered = render_full_snapshot(snapshot)

        # Every oltest_c5 running Project appears in the rendered live surface.
        surfaced = {r.project_id for r in snapshot.rows}
        for project_id in _RUNNING_FLEET:
            assert project_id in surfaced
            assert project_id in rendered
        # FR-062 stale marker + FR-063 non-binding framing are present.
        assert HEARTBEAT_STALLED in rendered
        assert COST_NON_BINDING_NOTE in rendered
        assert f"as of {_NOW.isoformat()}" in rendered

        # FR-058 / NFR-009: the build+render mutated no live lifecycle_state row.
        after = {p: _lifecycle(verify, p) for p in _RUNNING_FLEET}
        assert after == before == {p: "running" for p in _RUNNING_FLEET}
    finally:
        _restore(verify)
        live.close()
        verify.close()
