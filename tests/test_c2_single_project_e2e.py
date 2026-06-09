"""C2 single-project end-to-end integration checkpoint (OLB-08).

Drives the LIVE Outer-Loop Supervisor pipeline against the disposable Supabase dev
branch (ref ``jmjncijbbakuzndqhssw``) and a real spawned ``orchestrator.sh``,
asserting the five OLB-08 predicate facets (Spec v1.3 §6 admission, §5.4 Run
Registry, §14 teardown):

  (a) discover     — ``discover_candidates`` returns the ``oltest_c2`` Candidate
                     through the live OLB-02 RegistryPort (real branch read).
  (b) admit+spawn  — ``admit_candidate`` -> ``RunRecord``; ``projects.lifecycle_state``
                     reads ``running``, a ``ralph_runs`` row carries ``spawned_at``,
                     and the REAL spawned ``orchestrator_pid`` is persisted (FR-009,
                     via ``set_run_orchestrator_pid``) equal to the captured pid.
  (c) complete     — the spawned minimal ``oltest_c2`` harness drains its 1-gap
                     register to zero-open and emits §13.1 INITIATIVE_COMPLETE.
  (d) reconcile    — the §14 teardown ``reconcile_run('complete', terminated_at,
                     terminal_cost_usd)`` lands; the branch row reads ``complete``
                     with a non-null ``terminated_at`` and an exact-``Decimal``
                     ``terminal_cost_usd`` (FR-011 / FR-014 / NFR-007).
  (e) lifecycle    — ``projects.lifecycle_state='complete'`` (FR-008-legal
                     ``running -> complete``).

Live integration ONLY (gate ``olb08-c2-live-integration-fidelity`` = A): it wires
the existing OLB-02/06/07/08a seams through their public surfaces plus the live
``OrchestratorSpawnPort`` / ``SeedReviewValidator`` / ``run_lifecycle`` impls; it
changes no closed-item signature.

Cost/safety gating: this test spawns a real ``orchestrator.sh`` whose inner loop
runs real ``claude -p`` calls (bounded by the oltest_c2 seed: ``tokens_usd: 8``,
``iterations_max: 2``). It is therefore opt-in — collected on every run but SKIPPED
unless BOTH the branch DSN (``OL_SUPERVISOR_DB_URL``, gate olb08-c2-branch-connection
= A direct session-mode psycopg) AND the opt-in flag (``OLB_C2_LIVE=1``) are set,
so routine cumulative-regression runs never trigger an unintended real-LLM drain.
"""
from __future__ import annotations

import os
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from supervisor.admission import RunRecord, admit_candidate, discover_candidates
from supervisor.ports import RegistryRow
from supervisor.registry import Registry
from supervisor.run_lifecycle import reconcile_run_complete, resolve_run_terminal
from supervisor.safety_gates import READ_ONLY_CORPUS_PATH, KillSwitch
from supervisor.seed_validation import SeedReviewValidator
from supervisor.spawn import OrchestratorSpawnPort

pytestmark = pytest.mark.integration

# --- Disposable C2 substrate (the oltest_c2 harness + dev branch) ------------
PROJECT_ID = "oltest_c2"
OLTEST_C2_ROOT = Path(
    r"K:\Claude Code Factory\V3\Project_Docs\Sub_Projects\ol-build\oltest_c2"
)
SEED_PATH = OLTEST_C2_ROOT / "OLTest_C2_Harness_Seed_v1.0.md"
REGISTER_PATH = OLTEST_C2_ROOT / "OLTest_C2_Register_v1.0.md"
STATE_DIR = OLTEST_C2_ROOT / "state"
RALPH_DEV = Path(r"K:\Claude Code Factory\V3\Ralph-dev")
ORCHESTRATOR = RALPH_DEV / "orchestrator.sh"
# FR-034 read-only-corpus token. OLB-06's ``lists_read_only_corpus`` matches each
# declared read-only path against the bare ``READ_ONLY_CORPUS_PATH`` ("Project_Docs_Current\")
# via ancestor-or-equal; an absolute corpus path is NOT an ancestor of the bare token, so the
# Blast-Radius Scope must declare the corpus in exactly the form the closed OLB-06 invariant
# recognises. Use the production constant so the constructed scope conforms (and never drifts).
READ_ONLY_CORPUS = READ_ONLY_CORPUS_PATH

DB_URL_ENV = "OL_SUPERVISOR_DB_URL"
LIVE_OPT_IN_ENV = "OLB_C2_LIVE"
# Bounded wait for the drain. The live oltest_c2 drain is a real iterations_max:2
# orchestrator.sh whose inner ``claude -p`` role calls (planner/executor/consumer)
# have variable per-call latency plus MCP-server startup, so the wait must clear the
# seed's own hang budget (hang_timeout_seconds: 1800) with headroom rather than the
# inner-loop hang_timeout: 600. A first live drain completed in ~674s; a second was
# still draining (only partial spend) at the old 900s ceiling and was killed mid-run —
# i.e. 900s was marginal, not a pipeline defect. Sized to the 1800s seed hang budget so
# a slow-but-healthy drain reaches INITIATIVE_COMPLETE; the orchestrator is still killed
# + the run reported incomplete if it genuinely overruns.
SPAWN_TIMEOUT_S = 1800.0

_DSN = os.environ.get(DB_URL_ENV, "")
_OPT_IN = os.environ.get(LIVE_OPT_IN_ENV, "") not in ("", "0", "false", "False")

requires_live = pytest.mark.skipif(
    not (_DSN and _OPT_IN),
    reason=(
        f"C2 live spawn checkpoint requires {DB_URL_ENV} (branch DSN) AND opt-in "
        f"{LIVE_OPT_IN_ENV}=1 — it spawns a real orchestrator.sh + claude -p drain."
    ),
)


def _connect(*, autocommit: bool = False):
    import psycopg

    conn = psycopg.connect(_DSN)
    if autocommit:
        conn.autocommit = True
    return conn


def _reset_register_open(path: Path) -> None:
    """Force the C1 gap row's Priority cell back to ``**P1**`` (re-runnability)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for line in lines:
        if line.lstrip().startswith("| C1 |"):
            cells = line.split("|")
            if len(cells) > 4:
                cells[4] = " **P1** "
                line = "|".join(cells)
        out.append(line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _reset_substrate(conn) -> None:
    """Restore the clean precondition: candidate project, no active run, fresh
    state dir, the single gap re-opened."""
    cur = conn.cursor()
    cur.execute("DELETE FROM ralph_runs WHERE project_slug = %s", (PROJECT_ID,))
    cur.execute(
        "UPDATE projects SET lifecycle_state='candidate', status='active', "
        "updated_at=now() WHERE project_id = %s",
        (PROJECT_ID,),
    )
    conn.commit()
    if STATE_DIR.exists():
        shutil.rmtree(STATE_DIR)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _reset_register_open(REGISTER_PATH)


def _enriched_candidate(discovered: RegistryRow) -> dict[str, object]:
    """The discovered branch row + the seed-derived admission inputs.

    ``read_candidates`` surfaces only the ``projects`` columns, so the seed-derived
    fields the §6 Admission Gate reads (``seed_path`` / ``open_item_count`` /
    ``writable_paths`` / ``mcp_roots``) are merged in here from the oltest_c2 seed.
    This is test orchestration over the existing public seam — it edits no closed
    component.
    """
    row = dict(discovered)
    row.update(
        {
            "seed_path": str(SEED_PATH),
            "initiative_slug": PROJECT_ID,
            "open_item_count": 1,
            "writable_paths": [str(OLTEST_C2_ROOT)],
            "mcp_roots": [str(RALPH_DEV)],
            "read_only_paths": [READ_ONLY_CORPUS],
        }
    )
    return row


@requires_live
def test_c2_single_project_end_to_end() -> None:
    verify = _connect(autocommit=True)
    live_conn = _connect()
    try:
        _reset_substrate(verify)
        live_registry = Registry(live_conn)

        # (a) Discover — live branch read through the OLB-02 RegistryPort.
        candidates = discover_candidates(live_registry)
        discovered = next(
            (c for c in candidates if str(c["project_id"]) == PROJECT_ID), None
        )
        assert discovered is not None, (
            f"{PROJECT_ID} not discovered as a candidate; "
            f"got {[str(c['project_id']) for c in candidates]}"
        )
        candidate = _enriched_candidate(discovered)

        # Live ports. The known-clean oltest_c2 seed must carry no SEVERE finding.
        seed_validator = SeedReviewValidator()
        findings = seed_validator.validate_seed(candidate)
        assert not any(f.is_severe() for f in findings), f"seed not clean: {findings}"
        spawn_port = OrchestratorSpawnPort(ORCHESTRATOR)

        # (b) Admit + spawn — the only path Candidate -> running (FR-021).
        result = admit_candidate(
            candidate,
            seed_validator=seed_validator,
            registry_port=live_registry,
            spawn_port=spawn_port,
            kill_switch=KillSwitch(),
            running_count=0,
        )
        assert isinstance(result, RunRecord), f"admission did not spawn: {result!r}"
        assert spawn_port.last_handle is not None
        captured_pid = result.orchestrator_pid
        assert isinstance(captured_pid, int) and captured_pid > 0

        cur = verify.cursor()
        cur.execute(
            "SELECT lifecycle_state FROM projects WHERE project_id=%s", (PROJECT_ID,)
        )
        assert cur.fetchone()[0] == "running"
        cur.execute(
            "SELECT status, spawned_at, orchestrator_pid FROM ralph_runs "
            "WHERE project_slug=%s ORDER BY created_at DESC LIMIT 1",
            (PROJECT_ID,),
        )
        run_row = cur.fetchone()
        assert run_row is not None, "no ralph_runs row after admit_and_spawn"
        assert run_row[0] == "running"
        assert run_row[1] is not None, "spawned_at not persisted"
        assert run_row[2] == captured_pid, (
            f"persisted orchestrator_pid {run_row[2]} != captured pid {captured_pid}"
        )

        # (c) INITIATIVE_COMPLETE — wait for the real drain to terminal.
        handle = spawn_port.last_handle
        terminal = resolve_run_terminal(
            handle.process,
            STATE_DIR,
            timeout_s=SPAWN_TIMEOUT_S,
            stdout_path=handle.stdout_path,
        )
        assert terminal.completed, (
            f"orchestrator did not reach INITIATIVE_COMPLETE "
            f"(exit={terminal.exit_code}): {terminal.detail}"
        )

        # (d) Terminal reconcile — FR-011 terminated_at + FR-014/NFR-007 cost.
        reconcile_run_complete(live_registry, PROJECT_ID, terminal)
        cur.execute(
            "SELECT status, terminated_at, terminal_cost_usd FROM ralph_runs "
            "WHERE project_slug=%s ORDER BY created_at DESC LIMIT 1",
            (PROJECT_ID,),
        )
        rec = cur.fetchone()
        assert rec[0] == "complete"
        assert rec[1] is not None, "terminated_at not persisted on reconcile"
        assert isinstance(rec[2], Decimal), "terminal_cost_usd not an exact Decimal"
        assert rec[2] == terminal.terminal_cost_usd

        # (e) lifecycle complete — FR-008-legal running -> complete.
        cur.execute(
            "SELECT lifecycle_state FROM projects WHERE project_id=%s", (PROJECT_ID,)
        )
        assert cur.fetchone()[0] == "complete"
    finally:
        live_conn.close()
        verify.close()
