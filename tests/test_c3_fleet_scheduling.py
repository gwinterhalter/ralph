"""C3 cross-project fleet-scheduling integration checkpoint (OLB-11).

Drives the LIVE OLB-09 Cross-Project Scheduler + OLB-07/07x Admission Pipeline —
through the additively-wired §4.4 ``SupervisionCycle._schedule()`` host
(``supervisor/cycle_wiring.py``, gate ``olb11-c3-cycle-wiring-scope`` = A) — against
a 3-member fleet on the disposable Supabase dev branch (ref
``jmjncijbbakuzndqhssw``) under Concurrency Ceiling 2, asserting the four OLB-11
predicate facets against the ACTUAL branch ``projects`` / ``ralph_runs`` rows
(Spec v1.3 §7 / §6 / §9):

  (1) priority order        — the highest-``priority`` Candidate is dispatched first
                              (FR-023); the fleet runs A(30) -> B(20) before C(10).
  (2) ceiling hold          — two members reach ``running`` and the third is HELD
                              ``admitted`` once the ceiling is reached (FR-019 hold +
                              §9 FR-037 refusal); the live
                              ``uq_ralph_runs_active_per_project`` partial unique
                              index gates the second active Run per Project.
  (3) dispatch idempotency  — re-running the cycle creates no second ``running`` Run
                              for an in-flight Project (FR-027); the unique index
                              rejects a duplicate active Run insert directly.
  (4) gate-blocked skip     — a ``paused_gate`` member is never dispatched and its
                              scheduler skip-count is carried forward UNTOUCHED
                              (FR-026 — a block never demotes), via the persisted
                              :class:`RoundStateStore`.

Spawn fidelity (gate ``olb11-c3-fleet-substrate-and-spawn-fidelity`` = A): the
``orchestrator.sh`` -> ``claude -p`` spawn is STUBBED via an injected fake
:class:`SpawnPort` (records the spawn; no real LLM drain — harness-completion
fidelity was the C2/OLB-08 predicate, proven LIVE-green at iter-0021). Admission
therefore writes the real ``running`` ``ralph_runs`` rows the live unique index +
FR-037 ceiling gate, at near-zero LLM spend.

Live boundary: REAL branch rows + the real OLB-02 ``Registry`` (psycopg). Collected
on every run but SKIPPED unless ``OL_SUPERVISOR_DB_URL`` is set AND points at the
disposable branch (the production ref must be ABSENT) — a structural guarantee that
this checkpoint can never touch production ``eybdbshxswutgaaylpol``. The stubbed
spawn keeps it cheap + safe, so — unlike the C2 e2e — it needs no extra opt-in flag.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import cast

import psycopg
import pytest

from supervisor.admission import SeedFinding, SpawnResult
from supervisor.attention import Escalation, UrgencyTier
from supervisor.cycle import SupervisionCycle
from supervisor.cycle_wiring import (
    AttendConfig,
    AttentionStateStore,
    RoundStateStore,
    ScheduleConfig,
    run_attend_step,
)
from supervisor.ports import RegistryRow
from supervisor.registry import DBConnection, Registry
from supervisor.safety_gates import READ_ONLY_CORPUS_PATH, BlastRadiusScope, KillSwitch

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
        f"C3 live fleet checkpoint requires {DB_URL_ENV} pointing at the disposable "
        f"branch {BRANCH_REF} (production ref {PRODUCTION_REF} must be absent)."
    ),
)

pytestmark = [pytest.mark.integration, requires_branch]

# --- Disposable oltest_c3 fleet (priority-differentiated, Ceiling 2) ----------
PROJECT_A = "oltest_c3_a"  # priority 30 — dispatched first
PROJECT_B = "oltest_c3_b"  # priority 20 — dispatched second
PROJECT_C = "oltest_c3_c"  # priority 10 — held `admitted` at the ceiling
PROJECT_D = "oltest_c3_d"  # priority 25 — gate-blocked (`paused_gate`), never dispatched

CONCURRENCY_CEILING = 2
# Pre-seeded scheduler skip-count for the gate-blocked member; FR-026 requires it to
# survive a dispatch round untouched (a block never demotes).
GATE_BLOCKED_SKIP_COUNT = 4

RALPH_DEV = r"K:\Claude Code Factory\V3\Ralph-dev"
OLTEST_C3_ROOT = r"K:\Claude Code Factory\V3\Project_Docs\Sub_Projects\ol-build\oltest_c3"

# Provisioning rows: (project_id, priority, lifecycle_state).
_FLEET = (
    (PROJECT_A, 30, "candidate"),
    (PROJECT_B, 20, "candidate"),
    (PROJECT_C, 10, "candidate"),
    (PROJECT_D, 25, "paused_gate"),
)


# --- Live ports: a clean seed validator + a stubbed (no-drain) spawn port -----


class _CleanSeedValidator:
    """A :class:`SeedValidatorPort` returning no findings — the trivial oltest_c3
    seeds carry no SEVERE ``SS-*`` finding, so admission's FR-016 gate clears."""

    def validate_seed(self, candidate: RegistryRow) -> Sequence[SeedFinding]:
        return ()


class _StubSpawnPort:
    """A :class:`SpawnPort` that records the spawn and performs NO real
    ``orchestrator.sh`` / ``claude -p`` drain (gate olb11-...-spawn-fidelity = A).

    Returns ``ok=True`` with a synthetic positive pid so ``admit_and_spawn`` writes
    a real ``running`` ``ralph_runs`` row (the live unique index + FR-037 ceiling are
    exercised) without spending an LLM token."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._pid = 990_000

    def spawn(self, seed_path: str, blast_radius_scope: BlastRadiusScope) -> SpawnResult:
        self._pid += 1
        self.calls.append(seed_path)
        return SpawnResult(ok=True, orchestrator_pid=self._pid)


def _is_fleet_member(row: RegistryRow) -> bool:
    """Scope the checkpoint to its disposable oltest_c3 fleet only.

    The live branch carries other initiatives' Projects (e.g. ``rl_test`` /
    ``ol_build``); this predicate isolates the checkpoint's scheduler view to its own
    rows so the Ceiling-2 demonstration is controlled and no other initiative's live
    row is ever read, ranked, or (critically) spawned."""
    return str(row["project_id"]).startswith("oltest_c3_")


def _enrich_candidate(row: RegistryRow) -> RegistryRow:
    """Merge the seed-derived §6 admission inputs onto a discovered ``projects`` row.

    ``read_candidates`` surfaces only the ``projects`` columns, so the fields the
    Admission Gate reads (``seed_path`` / ``open_item_count`` / ``writable_paths`` /
    ``mcp_roots`` / ``read_only_paths``) are merged here (the C2 ``_enriched_candidate``
    pattern). ``read_only_paths`` uses the production ``READ_ONLY_CORPUS_PATH`` token so
    the FR-034 invariant clears; ``seed_path`` is recorded on the spawned Run row
    (``ralph_runs.seed_path`` NOT NULL) but never opened (the spawn is stubbed)."""
    project_id = str(row["project_id"])
    enriched = dict(row)
    enriched.update(
        {
            "seed_path": rf"{OLTEST_C3_ROOT}\{project_id}\seed.md",
            "initiative_slug": project_id,
            "open_item_count": 1,
            "writable_paths": [rf"{OLTEST_C3_ROOT}\{project_id}"],
            "mcp_roots": [RALPH_DEV],
            "read_only_paths": [READ_ONLY_CORPUS_PATH],
        }
    )
    return enriched


# --- Branch substrate provisioning + restore (row-state only; no reshape) ------


def _connect(*, autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(_DSN)
    if autocommit:
        conn.autocommit = True
    return conn


def _registry(conn: psycopg.Connection) -> Registry:
    """Build the live OLB-02 Registry over a branch connection.

    psycopg's ``Connection`` satisfies the Registry's narrow ``DBConnection`` surface
    structurally; the cast bridges the overload-shape gap the same way registry.py's
    own ``from_env`` factory relies on (the hermetic type env resolves it to ``Any``)."""
    return Registry(cast(DBConnection, conn))


def _provision_fleet(conn: psycopg.Connection) -> None:
    """Seed the disposable oltest_c3 fleet on the branch (clears any prior rows)."""
    cur = conn.cursor()
    cur.execute("DELETE FROM ralph_runs WHERE project_slug LIKE 'oltest_c3%%'")
    cur.execute("DELETE FROM projects WHERE project_id LIKE 'oltest_c3%%'")
    for project_id, priority, lifecycle_state in _FLEET:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, status, "
            "lifecycle_state, priority) VALUES (%s, %s, 'active', %s, %s)",
            (project_id, project_id, lifecycle_state, priority),
        )
    conn.commit()


def _restore_fleet(conn: psycopg.Connection) -> None:
    """Restore the pre-run branch state — the oltest_c3 rows were absent before this
    checkpoint, so deleting them (row-state only; no table reshape, Spec §4.2) is the
    exact restore."""
    cur = conn.cursor()
    cur.execute("DELETE FROM ralph_runs WHERE project_slug LIKE 'oltest_c3%%'")
    cur.execute("DELETE FROM projects WHERE project_id LIKE 'oltest_c3%%'")
    conn.commit()


def _lifecycle(conn: psycopg.Connection, project_id: str) -> str:
    cur = conn.cursor()
    cur.execute(
        "SELECT lifecycle_state FROM projects WHERE project_id = %s", (project_id,)
    )
    row = cur.fetchone()
    assert row is not None, f"no projects row for {project_id!r}"
    return str(row[0])


def _running_run_count(conn: psycopg.Connection, project_id: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM ralph_runs WHERE project_slug = %s AND status = 'running'",
        (project_id,),
    )
    row = cur.fetchone()
    assert row is not None
    return int(row[0])


# --- (1)-(4) the fleet drive under Concurrency Ceiling 2 ----------------------


@pytest.mark.integration
def test_c3_fleet_scheduling_holds_ceiling_with_priority_order() -> None:
    """OLB-11 predicate (1)-(4): driving the wired cycle host over the oltest_c3 fleet
    under Ceiling 2, two members run in priority order and the third is held
    ``admitted``; re-dispatch is idempotent; the gate-blocked member is skipped without
    demotion — all asserted against actual branch rows. Covers FR-019/022–027/037."""
    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision_fleet(verify)
        registry = _registry(live)
        spawn = _StubSpawnPort()
        round_state = RoundStateStore(initial={PROJECT_D: GATE_BLOCKED_SKIP_COUNT})
        config = ScheduleConfig(
            seed_validator=_CleanSeedValidator(),
            spawn_port=spawn,
            kill_switch=KillSwitch(),
            concurrency_ceiling=CONCURRENCY_CEILING,
            round_state_store=round_state,
            open_work_counts={PROJECT_A: 1, PROJECT_B: 1, PROJECT_C: 1},
            candidate_enricher=_enrich_candidate,
            project_filter=_is_fleet_member,
        )
        cycle = SupervisionCycle(registry, schedule_config=config)

        # (1) Round 1 — priority order: the highest-priority Candidate A is first.
        cycle.run_once()
        assert _lifecycle(verify, PROJECT_A) == "running"
        assert _lifecycle(verify, PROJECT_B) == "candidate"
        assert _lifecycle(verify, PROJECT_C) == "candidate"
        assert _running_run_count(verify, PROJECT_A) == 1
        assert len(spawn.calls) == 1

        # Round 2 — B (next priority) dispatched; two members now running.
        cycle.run_once()
        assert _lifecycle(verify, PROJECT_A) == "running"
        assert _lifecycle(verify, PROJECT_B) == "running"
        assert _lifecycle(verify, PROJECT_C) == "candidate"
        assert _running_run_count(verify, PROJECT_B) == 1
        assert len(spawn.calls) == 2

        # (2) Round 3 — ceiling hold: C is HELD `admitted`, nothing spawned (FR-019 +
        #     FR-037); exactly two running Runs across the fleet.
        cycle.run_once()
        assert _lifecycle(verify, PROJECT_A) == "running"
        assert _lifecycle(verify, PROJECT_B) == "running"
        assert _lifecycle(verify, PROJECT_C) == "admitted"
        assert _running_run_count(verify, PROJECT_C) == 0
        assert len(spawn.calls) == 2  # C was held — no spawn

        # (3) Round 4 — dispatch idempotency: re-running issues no new Run (FR-027);
        #     each running Project still has exactly one active Run.
        cycle.run_once()
        assert _running_run_count(verify, PROJECT_A) == 1
        assert _running_run_count(verify, PROJECT_B) == 1
        assert _running_run_count(verify, PROJECT_C) == 0
        assert _lifecycle(verify, PROJECT_C) == "admitted"
        assert len(spawn.calls) == 2

        # (3) the live `uq_ralph_runs_active_per_project` index rejects a second active
        #     Run for an already-running Project — the ceiling/idempotency backstop.
        with pytest.raises(psycopg.errors.UniqueViolation):
            dup = verify.cursor()
            dup.execute(
                "INSERT INTO ralph_runs (project_slug, seed_path, status) "
                "VALUES (%s, %s, 'running')",
                (PROJECT_A, "dup-seed"),
            )

        # (4) gate-blocked skip without demotion: D was never dispatched and stays
        #     `paused_gate`; its persisted skip-count is carried forward UNTOUCHED.
        assert _lifecycle(verify, PROJECT_D) == "paused_gate"
        assert _running_run_count(verify, PROJECT_D) == 0
        assert round_state.load().get(PROJECT_D) == GATE_BLOCKED_SKIP_COUNT
    finally:
        _restore_fleet(verify)
        live.close()
        verify.close()


# --- §4.4 step-4 Attend hook (gate 0002 = A wires _attend too) ----------------


@pytest.mark.integration
def test_c3_attend_hook_intakes_escalation_and_plans_notification() -> None:
    """Gate olb11-c3-cycle-wiring-scope = A wires the §4.4 step-4 Attend hook too:
    ``run_once()`` intakes a newly-raised escalation (FR-028 — Attention Debt +1, no
    double-intake on a second pass) and a notification plan collapses the queued
    routine escalation into a single batch (FR-029/030)."""
    live = _connect()
    try:
        registry = _registry(live)
        raised_at = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
        now = datetime(2026, 6, 5, 12, 30, tzinfo=timezone.utc)
        escalation = Escalation(
            project_id=PROJECT_D,
            gate_id="oltest_c3_gate_0001",
            kind="routine",
            reversible=True,
            suggested_option="A",
            confidence=0.9,
            raised_at=raised_at,
        )
        store = AttentionStateStore()
        pending: list[Escalation] = [escalation]
        attend_config = AttendConfig(
            attention_store=store,
            quiet_hours=None,
            batch_window=timedelta(hours=1),
            clock=lambda: now,
            incoming=lambda: tuple(pending),
        )
        cycle = SupervisionCycle(registry, attend_config=attend_config)

        # FR-028 — the Attend hook intakes the escalation: Attention Debt for D = 1.
        cycle.run_once()
        assert store.load().debt_for(PROJECT_D) == 1

        # No double-intake when nothing new is raised on the next pass.
        pending.clear()
        cycle.run_once()
        assert store.load().debt_for(PROJECT_D) == 1

        # FR-029/030 — the queued routine escalation surfaces as a single routine batch.
        plan = run_attend_step(registry, attend_config)
        assert len(plan.batches) == 1
        batch = plan.batches[0]
        assert batch.tier == UrgencyTier.ROUTINE
        assert [e.project_id for e in batch.escalations] == [PROJECT_D]
        assert store.load().debt_for(PROJECT_D) == 1  # plan-only; no new intake
    finally:
        live.close()
