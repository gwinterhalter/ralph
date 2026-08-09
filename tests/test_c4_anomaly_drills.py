"""C4 anomaly-drills integration checkpoint (OLB-14).

Exercises the supervisor's guard-time anomaly response — through the additively-wired
§4.4 ``SupervisionCycle._guard()`` host (``supervisor/cycle_wiring.py``, gate
``olb14-c4-guard-wiring-scope`` = A) — against disposable ``oltest_c4_*`` Projects on
the disposable Supabase dev branch (ref ``jmjncijbbakuzndqhssw``), asserting the OLB-14
predicate facets against the ACTUAL branch ``projects`` / ``ralph_runs`` rows
(Spec v1.3 §9 / §10 / §11):

  Stall drill (§11 Repair-Auto-OK Policy):
    * a reversible, in-scope, confidence-met stall repair is GRANTED autonomously
      (FR-045) — the Run continues, no trip, no escalation, an FR-047 audit record is
      built; and
    * an irreversible (out-of-scope / below-threshold) stall is ESCALATED — the Project
      is tripped to ``paused_safety`` with a top-tier escalation (FR-046 / FR-038), the
      Run is NOT terminated.

  Spend-anomaly drill (§10 Cost Circuit-Breaker):
    * a crafted spend history whose latest iteration blows out the trailing median trips
      the breaker (FR-039) → the Project is moved to ``paused_safety`` + a top-tier
      escalation (FR-029); and
    * a sibling Project with a benign history is UNAFFECTED (FR-043 per-Project isolation).

  No-silent-kill capstone (§9 FR-038):
    * across both drills, every trip resolves to ``paused_safety`` + an escalation — no
      ``oltest_c4`` Run row is ever moved to a terminal / killed status, and no Project
      row is moved to ``failed`` by a trip.

Substrate + fault fidelity (gate ``olb14-c4-anomaly-drill-substrate-and-fault-fidelity``
= A): deterministic fault injection on the live branch — the stall signal and the spend
history are constructed inputs (no real PID-kill, no real LLM drain); the spawn is
stubbed (no ``ralph_runs`` Run is ever drained). The C4 predicate verifies the
guard-time anomaly-RESPONSE decision logic, observable against real branch rows at
near-zero LLM spend — harness completion (the C2/OLB-08 predicate) and scheduling
throughput (the C3/OLB-11 predicate) were proven LIVE-green at iter-0021 / iter-0025.

Live boundary: REAL branch rows + the real OLB-02 ``Registry`` (psycopg). Collected on
every run but SKIPPED unless ``OL_SUPERVISOR_DB_URL`` is set AND points at the disposable
branch (the production ref must be ABSENT) — a structural guarantee that this checkpoint
can never touch production ``eybdbshxswutgaaylpol``.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import psycopg
import pytest

from supervisor.attention import UrgencyTier, assign_urgency_tier
from supervisor.cost_circuit_breaker import (
    BreakerConfig,
    IterationObservation,
    TripKind,
)
from supervisor.cycle import SupervisionCycle
from supervisor.cycle_wiring import (
    AttentionStateStore,
    GuardConfig,
    StallSignal,
    run_guard_step,
)
from supervisor.ports import RegistryRow
from supervisor.registry import DBConnection, Registry
from supervisor.repair_policy import RepairKind, ReversibilityClass

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
        f"C4 live anomaly-drills checkpoint requires {DB_URL_ENV} pointing at the "
        f"disposable branch {BRANCH_REF} (production ref {PRODUCTION_REF} must be absent)."
    ),
)

pytestmark = [pytest.mark.integration, requires_branch]

# --- Disposable oltest_c4 Projects -------------------------------------------
PROJECT_REVERSIBLE = "oltest_c4_reversible"  # stall -> reversible repair, granted
PROJECT_IRREVERSIBLE = "oltest_c4_irreversible"  # stall -> irreversible, escalated
PROJECT_SPEND = "oltest_c4_spend"  # spend-delta anomaly -> breaker trip
PROJECT_SIBLING = "oltest_c4_sibling"  # benign history -> unaffected (FR-043)

# Every drilled Project is provisioned `running` with one active `ralph_runs` row so the
# no-silent-kill capstone can assert the Run row is never moved off `running` by a trip.
_FLEET = (
    PROJECT_REVERSIBLE,
    PROJECT_IRREVERSIBLE,
    PROJECT_SPEND,
    PROJECT_SIBLING,
)

# FR-039 thresholds: the spend-delta multiple + trailing window are the only active
# detector; spend_without_closure_k / target_loop_j are set beyond the crafted history
# length so only the single-iteration blow-out can fire (no accidental K/J trip).
_BREAKER_CONFIG = BreakerConfig(
    spend_delta_multiple=Decimal(3),
    trailing_window=3,
    spend_without_closure_k=99,
    target_loop_j=99,
)

# The seed-config confidence threshold the §11 grant boundary is parameterised against
# (seed v1.6.2 gate_policy.confidence_threshold = 0.7).
_CONFIDENCE_THRESHOLD = 0.7

_RAISED_AT = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)


# --- Branch substrate provisioning + restore (row-state only; no reshape) ------


def _connect(*, autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(_DSN)
    if autocommit:
        conn.autocommit = True
    return conn


def _registry(conn: psycopg.Connection) -> Registry:
    """Build the live OLB-02 Registry over a branch connection (the C3 cast bridge)."""
    return Registry(cast(DBConnection, conn))


def _provision(conn: psycopg.Connection, project_ids: Sequence[str]) -> None:
    """Seed the named disposable oltest_c4 Projects `running` with one active Run each.

    Clears any prior oltest_c4 rows first, then inserts a `running` `projects` row + a
    `running` `ralph_runs` row (a valid non-null `seed_path`, OLB-07x shape) per id, so
    the guard reads them as the running fleet and the capstone can prove the Run row is
    never killed by a trip."""
    cur = conn.cursor()
    cur.execute("DELETE FROM ralph_runs WHERE project_slug LIKE 'oltest_c4%%'")
    cur.execute("DELETE FROM projects WHERE project_id LIKE 'oltest_c4%%'")
    for project_id in project_ids:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, status, lifecycle_state) "
            "VALUES (%s, %s, 'active', 'running')",
            (project_id, project_id),
        )
        cur.execute(
            "INSERT INTO ralph_runs (project_slug, seed_path, status) "
            "VALUES (%s, %s, 'running')",
            (project_id, f"oltest_c4://{project_id}/seed.md"),
        )
    conn.commit()


def _restore(conn: psycopg.Connection) -> None:
    """Restore the pre-run branch state — the oltest_c4 rows were absent before this
    checkpoint, so deleting them (row-state only; no table reshape, Spec §4.2) is the
    exact restore."""
    cur = conn.cursor()
    cur.execute("DELETE FROM ralph_runs WHERE project_slug LIKE 'oltest_c4%%'")
    cur.execute("DELETE FROM projects WHERE project_id LIKE 'oltest_c4%%'")
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
    """Scope the guard to its own disposable oltest_c4 Projects only.

    The live branch carries other initiatives' Projects (e.g. ``ol_build`` / ``rl_test`` /
    ``oltest_c3``); this predicate isolates the guard's running read to the checkpoint's
    own rows so no other initiative's live row is ever read, tripped, or escalated."""
    return str(row["project_id"]).startswith("oltest_c4_")


def _spend_history(
    project_id: str, spends: Sequence[str], *, open_count: int = 1
) -> tuple[IterationObservation, ...]:
    """Build a single Project's iteration spend history (money as Decimal, NFR-007).

    ``open_count`` is held constant and no ``target_id`` is set, so neither the FR-040
    spend-without-closure nor the FR-041 target-loop detector can fire — only the FR-039
    single-iteration spend-delta is in play (and only when the latest spend blows out)."""
    return tuple(
        IterationObservation(
            project_id=project_id,
            iteration_index=index,
            spend_usd=Decimal(spend),
            open_count=open_count,
        )
        for index, spend in enumerate(spends, start=1)
    )


# --- Stall drill: §11 Repair-Auto-OK Policy (FR-044 / FR-045 / FR-046) --------


@pytest.mark.integration
def test_c4_stall_reversible_grants_autonomously_irreversible_escalates() -> None:
    """OLB-14 stall facet: an injected stall is routed by the Repair-Auto-OK Policy — a
    reversible, in-scope, confidence-met repair is granted autonomously (FR-045, the Run
    continues, no trip); an irreversible repair is escalated to ``paused_safety`` + a
    top-tier escalation (FR-046 / FR-038), the Run NOT terminated. Asserted against actual
    branch rows. Covers FR-044 / FR-045 / FR-046 / FR-038."""
    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision(verify, (PROJECT_REVERSIBLE, PROJECT_IRREVERSIBLE))
        registry = _registry(live)
        store = AttentionStateStore()
        signals = {
            # reversible re-attach, in scope, confidence >= threshold -> GRANT (FR-045).
            PROJECT_REVERSIBLE: StallSignal(
                repair_kind=RepairKind.REATTACH_STALLED_RUN,
                triggering_anomaly="stall: no orchestrator_pid progress past hang_timeout",
                confidence=0.9,
            ),
            # discard/rewrite committed state is irreversible -> ESCALATE (FR-046).
            PROJECT_IRREVERSIBLE: StallSignal(
                repair_kind=RepairKind.DISCARD_OR_REWRITE_COMMITTED,
                triggering_anomaly="stall: committed-state rewrite proposed",
                confidence=0.9,
            ),
        }
        config = GuardConfig(
            stall_signals=lambda: signals,
            confidence_threshold=_CONFIDENCE_THRESHOLD,
            attention_store=store,
            clock=lambda: _RAISED_AT,
            project_filter=_is_fleet_member,
        )

        outcome = run_guard_step(registry, config)

        # FR-045: the reversible repair was granted autonomously — Run continues, no trip.
        assert _lifecycle(verify, PROJECT_REVERSIBLE) == "running"
        assert _run_status(verify, PROJECT_REVERSIBLE) == "running"
        assert PROJECT_REVERSIBLE not in outcome.paused_projects
        granted_ids = [rec.action.project_id for rec in outcome.granted_repairs]
        assert granted_ids == [PROJECT_REVERSIBLE]
        assert outcome.granted_repairs[0].reversibility is ReversibilityClass.REVERSIBLE
        # FR-045: a granted repair raises no escalation (Attention Debt stays 0).
        assert store.load().debt_for(PROJECT_REVERSIBLE) == 0

        # FR-046 / FR-038: the irreversible repair was escalated — paused_safety + a
        # top-tier escalation, the Run NOT terminated.
        assert _lifecycle(verify, PROJECT_IRREVERSIBLE) == "paused_safety"
        assert _run_status(verify, PROJECT_IRREVERSIBLE) == "running"  # no kill
        assert outcome.paused_projects == (PROJECT_IRREVERSIBLE,)
        assert store.load().debt_for(PROJECT_IRREVERSIBLE) == 1
        escalations = [e for e in outcome.escalations if e.project_id == PROJECT_IRREVERSIBLE]
        assert len(escalations) == 1
        assert assign_urgency_tier(escalations[0]) is UrgencyTier.TOP
    finally:
        _restore(verify)
        live.close()
        verify.close()


# --- Spend-anomaly drill: §10 Cost Circuit-Breaker (FR-039 / FR-043 / FR-029) -


@pytest.mark.integration
def test_c4_spend_anomaly_trips_breaker_and_isolates_sibling() -> None:
    """OLB-14 spend facet: a crafted spend history whose latest iteration blows out the
    trailing median trips the Cost Circuit-Breaker (FR-039) — the Project is moved to
    ``paused_safety`` + a top-tier escalation (FR-029), the Run NOT terminated; a sibling
    with a benign history is UNAFFECTED (FR-043). Asserted against actual branch rows."""
    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision(verify, (PROJECT_SPEND, PROJECT_SIBLING))
        registry = _registry(live)
        store = AttentionStateStore()
        histories = {
            # latest iteration (10) > 3x the trailing median of [1,1,1] -> FR-039 trip.
            PROJECT_SPEND: _spend_history(PROJECT_SPEND, ["1", "1", "1", "10"]),
            # flat history -> within threshold -> no anomaly (FR-043 isolation).
            PROJECT_SIBLING: _spend_history(PROJECT_SIBLING, ["1", "1", "1", "1"]),
        }
        config = GuardConfig(
            breaker_config=_BREAKER_CONFIG,
            spend_histories=lambda: histories,
            attention_store=store,
            clock=lambda: _RAISED_AT,
            project_filter=_is_fleet_member,
        )

        outcome = run_guard_step(registry, config)

        # FR-039 / FR-029 / FR-038: the spend Project tripped to paused_safety + a
        # top-tier escalation; the Run was NOT killed.
        assert _lifecycle(verify, PROJECT_SPEND) == "paused_safety"
        assert _run_status(verify, PROJECT_SPEND) == "running"  # no kill
        assert outcome.paused_projects == (PROJECT_SPEND,)
        assert len(outcome.breaker_trips) == 1
        trip = outcome.breaker_trips[0]
        assert trip.project_id == PROJECT_SPEND
        assert trip.kind is TripKind.SPEND_DELTA_ANOMALY
        escalations = [e for e in outcome.escalations if e.project_id == PROJECT_SPEND]
        assert len(escalations) == 1
        assert assign_urgency_tier(escalations[0]) is UrgencyTier.TOP
        assert store.load().debt_for(PROJECT_SPEND) == 1

        # FR-043: the sibling was evaluated independently and is unaffected.
        assert _lifecycle(verify, PROJECT_SIBLING) == "running"
        assert _run_status(verify, PROJECT_SIBLING) == "running"
        assert PROJECT_SIBLING not in outcome.paused_projects
        assert store.load().debt_for(PROJECT_SIBLING) == 0
    finally:
        _restore(verify)
        live.close()
        verify.close()


# --- The wired §4.4 Guard host path (gate 0002 = A) --------------------------


@pytest.mark.integration
def test_c4_guard_host_runs_both_drills_via_wired_cycle() -> None:
    """Gate ``olb14-c4-guard-wiring-scope`` = A wires the §4.4 step-5 Guard hook: driving
    the whole combined fleet through ``SupervisionCycle.run_once()`` applies BOTH anomaly
    responses in one pass — the spend Project and the irreversible-stall Project trip to
    ``paused_safety`` while the reversible-stall Project and the benign sibling keep
    running — proving the response works at the §4.4 host layer, not only the components."""
    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision(verify, _FLEET)
        registry = _registry(live)
        store = AttentionStateStore()
        histories = {
            PROJECT_SPEND: _spend_history(PROJECT_SPEND, ["1", "1", "1", "10"]),
            PROJECT_SIBLING: _spend_history(PROJECT_SIBLING, ["1", "1", "1", "1"]),
        }
        signals = {
            PROJECT_REVERSIBLE: StallSignal(
                repair_kind=RepairKind.REATTACH_STALLED_RUN,
                triggering_anomaly="stall: no orchestrator_pid progress",
                confidence=0.9,
            ),
            PROJECT_IRREVERSIBLE: StallSignal(
                repair_kind=RepairKind.DISCARD_OR_REWRITE_COMMITTED,
                triggering_anomaly="stall: committed-state rewrite proposed",
                confidence=0.9,
            ),
        }
        config = GuardConfig(
            breaker_config=_BREAKER_CONFIG,
            spend_histories=lambda: histories,
            stall_signals=lambda: signals,
            confidence_threshold=_CONFIDENCE_THRESHOLD,
            attention_store=store,
            clock=lambda: _RAISED_AT,
            project_filter=_is_fleet_member,
        )
        cycle = SupervisionCycle(registry, guard_config=config)

        # run_once() runs the six §4.4 steps; the wired Guard step applies the response.
        cycle.run_once()

        assert _lifecycle(verify, PROJECT_SPEND) == "paused_safety"
        assert _lifecycle(verify, PROJECT_IRREVERSIBLE) == "paused_safety"
        assert _lifecycle(verify, PROJECT_REVERSIBLE) == "running"
        assert _lifecycle(verify, PROJECT_SIBLING) == "running"
        # Two top-tier escalations queued (the spend trip + the irreversible-stall trip).
        queued = store.load().queue
        assert {e.project_id for e in queued} == {PROJECT_SPEND, PROJECT_IRREVERSIBLE}
        assert all(assign_urgency_tier(e) is UrgencyTier.TOP for e in queued)
    finally:
        _restore(verify)
        live.close()
        verify.close()


# --- No-silent-kill capstone: §9 FR-038 --------------------------------------


@pytest.mark.integration
def test_c4_no_trip_silently_kills_a_run() -> None:
    """OLB-14 no-silent-kill capstone (FR-038): across BOTH drills in one combined pass,
    every trip resolves to ``paused_safety`` + an escalation — no ``oltest_c4`` Run row is
    ever moved off ``running`` to a terminal / killed status, and no tripped Project row is
    moved to ``failed``. The §9 invariant that a safety trip pauses, never kills."""
    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision(verify, _FLEET)
        registry = _registry(live)
        histories = {
            PROJECT_SPEND: _spend_history(PROJECT_SPEND, ["1", "1", "1", "10"]),
            PROJECT_SIBLING: _spend_history(PROJECT_SIBLING, ["1", "1", "1", "1"]),
        }
        signals = {
            PROJECT_REVERSIBLE: StallSignal(
                repair_kind=RepairKind.REATTACH_STALLED_RUN,
                triggering_anomaly="stall: reattach",
                confidence=0.9,
            ),
            PROJECT_IRREVERSIBLE: StallSignal(
                repair_kind=RepairKind.DISCARD_OR_REWRITE_COMMITTED,
                triggering_anomaly="stall: committed rewrite",
                confidence=0.9,
            ),
        }
        config = GuardConfig(
            breaker_config=_BREAKER_CONFIG,
            spend_histories=lambda: histories,
            stall_signals=lambda: signals,
            confidence_threshold=_CONFIDENCE_THRESHOLD,
            attention_store=AttentionStateStore(),
            clock=lambda: _RAISED_AT,
            project_filter=_is_fleet_member,
        )

        outcome = run_guard_step(registry, config)

        # Both anomalous Projects tripped; the two healthy ones did not.
        assert set(outcome.paused_projects) == {PROJECT_SPEND, PROJECT_IRREVERSIBLE}

        # FR-038: NOT ONE oltest_c4 Run row was moved off `running` by a trip — no kill,
        # no terminal status anywhere in the fleet.
        cur = verify.cursor()
        cur.execute(
            "SELECT project_slug, status FROM ralph_runs "
            "WHERE project_slug LIKE 'oltest_c4%%'"
        )
        run_rows = cur.fetchall()
        assert len(run_rows) == len(_FLEET)
        assert all(str(status) == "running" for _slug, status in run_rows)

        # Every tripped Project is `paused_safety` (never `failed`); the healthy ones run.
        assert _lifecycle(verify, PROJECT_SPEND) == "paused_safety"
        assert _lifecycle(verify, PROJECT_IRREVERSIBLE) == "paused_safety"
        assert _lifecycle(verify, PROJECT_REVERSIBLE) == "running"
        assert _lifecycle(verify, PROJECT_SIBLING) == "running"
    finally:
        _restore(verify)
        live.close()
        verify.close()
