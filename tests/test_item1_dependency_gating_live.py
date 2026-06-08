"""Item 1 — cross-initiative dependency gating, LIVE integration checkpoint.

Drives the real OLB-02 ``Registry`` + the wired ``run_schedule_step`` against actual
``projects`` rows on the disposable Supabase dev branch (ref ``jmjncijbbakuzndqhssw``),
proving the dependency hold end-to-end over Postgres (the hermetic tests in
``test_dependency_gating.py`` / ``test_supervisor_admission.py`` cover the decision logic
against fakes; THIS asserts the real ``depends_on text[]`` round-trip + the live
``read_completed_project_ids`` read):

  (1) blocked while prereq incomplete — B ``depends_on`` A; A is dispatched, B is HELD
      (left ``candidate``, no Run row) — the dependency block, not the ceiling;
  (2) unblocked once prereq complete  — with A ``complete``, B is dispatched.

Spawn is STUBBED (no real orchestrator / LLM drain), exactly as the C3 checkpoint. Skipped
unless ``OL_SUPERVISOR_DB_URL`` points at the disposable branch (production ref absent), so it
can never touch production. REQUIRES the ``depends_on`` migration applied to the branch.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from typing import cast

import psycopg
import pytest

from supervisor.admission import SeedFinding, SpawnResult
from supervisor.cycle_wiring import ScheduleConfig, run_schedule_step
from supervisor.ports import RegistryRow
from supervisor.registry import DBConnection, Registry
from supervisor.safety_gates import READ_ONLY_CORPUS_PATH, BlastRadiusScope

DB_URL_ENV = "OL_SUPERVISOR_DB_URL"
BRANCH_REF = "jmjncijbbakuzndqhssw"
PRODUCTION_REF = "eybdbshxswutgaaylpol"
_DSN = os.environ.get(DB_URL_ENV, "")
_ON_BRANCH = bool(_DSN) and BRANCH_REF in _DSN and PRODUCTION_REF not in _DSN

requires_branch = pytest.mark.skipif(
    not _ON_BRANCH,
    reason=(
        f"Item 1 live checkpoint requires {DB_URL_ENV} pointing at the disposable branch "
        f"{BRANCH_REF} (production ref {PRODUCTION_REF} must be absent)."
    ),
)
pytestmark = [pytest.mark.integration, requires_branch]

PREREQ = "oltest_dep_a"  # the prerequisite
DEPENDENT = "oltest_dep_b"  # depends_on the prerequisite
RALPH_DEV = r"K:\Claude Code Factory\V3\Ralph-dev"
OLTEST_ROOT = r"K:\Claude Code Factory\V3\Project_Docs\Sub_Projects\ol-build\oltest_dep"


class _CleanSeedValidator:
    def validate_seed(self, candidate: RegistryRow) -> Sequence[SeedFinding]:
        return ()


class _StubSpawnPort:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._pid = 980_000

    def spawn(self, seed_path: str, blast_radius_scope: BlastRadiusScope) -> SpawnResult:
        self._pid += 1
        self.calls.append(seed_path)
        return SpawnResult(ok=True, orchestrator_pid=self._pid)


def _is_member(row: RegistryRow) -> bool:
    return str(row["project_id"]).startswith("oltest_dep_")


def _enrich(row: RegistryRow) -> RegistryRow:
    project_id = str(row["project_id"])
    enriched = dict(row)
    enriched.update(
        {
            "seed_path": rf"{OLTEST_ROOT}\{project_id}\seed.md",
            "initiative_slug": project_id,
            "open_item_count": 1,
            "writable_paths": [rf"{OLTEST_ROOT}\{project_id}"],
            "mcp_roots": [RALPH_DEV],
            "read_only_paths": [READ_ONLY_CORPUS_PATH],
        }
    )
    return enriched


def _connect(*, autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(_DSN)
    if autocommit:
        conn.autocommit = True
    return conn


def _registry(conn: psycopg.Connection) -> Registry:
    return Registry(cast(DBConnection, conn))


def _provision(conn: psycopg.Connection) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM ralph_runs WHERE project_slug LIKE 'oltest_dep_%%'")
    cur.execute("DELETE FROM projects WHERE project_id LIKE 'oltest_dep_%%'")
    cur.execute(
        "INSERT INTO projects (project_id, display_name, status, lifecycle_state, priority, "
        "depends_on) VALUES (%s, %s, 'active', 'candidate', 50, %s)",
        (PREREQ, PREREQ, []),
    )
    cur.execute(
        "INSERT INTO projects (project_id, display_name, status, lifecycle_state, priority, "
        "depends_on) VALUES (%s, %s, 'active', 'candidate', 50, %s)",
        (DEPENDENT, DEPENDENT, [PREREQ]),
    )
    conn.commit()


def _restore(conn: psycopg.Connection) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM ralph_runs WHERE project_slug LIKE 'oltest_dep_%%'")
    cur.execute("DELETE FROM projects WHERE project_id LIKE 'oltest_dep_%%'")
    conn.commit()


def _lifecycle(conn: psycopg.Connection, project_id: str) -> str:
    cur = conn.cursor()
    cur.execute("SELECT lifecycle_state FROM projects WHERE project_id = %s", (project_id,))
    row = cur.fetchone()
    assert row is not None
    return str(row[0])


def _config(registry: Registry, spawn: _StubSpawnPort) -> ScheduleConfig:
    return ScheduleConfig(
        seed_validator=_CleanSeedValidator(),  # type: ignore[arg-type]
        spawn_port=spawn,  # type: ignore[arg-type]
        candidate_enricher=_enrich,
        project_filter=_is_member,
        completed_project_ids=registry.read_completed_project_ids,
        concurrency_ceiling=2,
    )


def test_item1_dependency_hold_then_release_live() -> None:
    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision(verify)
        registry = _registry(live)
        spawn = _StubSpawnPort()
        config = _config(registry, spawn)

        # Round 1: A unblocked, B depends on the incomplete A -> only A dispatches.
        run_schedule_step(registry, config)
        run_schedule_step(registry, config)  # idempotent re-run; A in-flight, B still blocked
        assert _lifecycle(verify, PREREQ) == "running"
        assert _lifecycle(verify, DEPENDENT) == "candidate"  # HELD, never spawned
        assert all(DEPENDENT not in seed for seed in spawn.calls)

        # Complete the prerequisite, then B becomes runnable.
        verify.cursor().execute(
            "UPDATE projects SET lifecycle_state = 'complete' WHERE project_id = %s", (PREREQ,)
        )
        verify.commit()
        run_schedule_step(registry, config)
        assert _lifecycle(verify, DEPENDENT) == "running"  # released once prereq complete
    finally:
        _restore(verify)
        verify.close()
        live.close()
