"""Item 2 — Learn step over the live completed-Run source, LIVE integration checkpoint.

Inserts terminal ``ralph_runs`` rows on the disposable Supabase dev branch (ref
``jmjncijbbakuzndqhssw``), then exercises the REAL ``Registry.read_completed_runs`` +
``learn_assembly`` + ``run_learn_step`` over them — proving the SQL round-trip and the live
Learn pass (the hermetic ``test_learn_assembly.py`` / ``test_supervisor_registry.py`` cover the
mapping against fakes). Read-only audit → no LLM spend. Skipped unless ``OL_SUPERVISOR_DB_URL``
points at the disposable branch (production ref absent).
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import cast

import psycopg
import pytest

from supervisor.cycle_wiring import LearnConfig, run_learn_step
from supervisor.learn_assembly import (
    completed_run_records,
    learning_records,
    render_learning_corpus,
)
from supervisor.registry import DBConnection, Registry
from supervisor.run_auditor import RunAuditReport

DB_URL_ENV = "OL_SUPERVISOR_DB_URL"
BRANCH_REF = "jmjncijbbakuzndqhssw"
PRODUCTION_REF = "eybdbshxswutgaaylpol"
_DSN = os.environ.get(DB_URL_ENV, "")
_ON_BRANCH = bool(_DSN) and BRANCH_REF in _DSN and PRODUCTION_REF not in _DSN

requires_branch = pytest.mark.skipif(
    not _ON_BRANCH,
    reason=(
        f"Item 2 live checkpoint requires {DB_URL_ENV} pointing at the disposable branch "
        f"{BRANCH_REF} (production ref {PRODUCTION_REF} must be absent)."
    ),
)
pytestmark = [pytest.mark.integration, requires_branch]

SLUG = "oltest_learn_x"
SEED = r"K:\Claude Code Factory\V3\Project_Docs\Sub_Projects\ol-build\oltest_learn\seed.md"


def _connect(*, autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(_DSN)
    if autocommit:
        conn.autocommit = True
    return conn


def _provision(conn: psycopg.Connection) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM ralph_runs WHERE project_slug = %s", (SLUG,))
    # Two terminal Runs: one complete (with cost + duration), one failed.
    cur.execute(
        "INSERT INTO ralph_runs (project_slug, seed_path, status, spawned_at, terminated_at, "
        "terminal_cost_usd) VALUES (%s, %s, 'complete', "
        "'2026-06-07T10:00:00+00:00', '2026-06-07T10:05:00+00:00', %s)",
        (SLUG, SEED, Decimal("1.2500")),
    )
    cur.execute(
        "INSERT INTO ralph_runs (project_slug, seed_path, status, spawned_at, terminated_at, "
        "terminal_cost_usd) VALUES (%s, %s, 'failed', "
        "'2026-06-07T11:00:00+00:00', '2026-06-07T11:02:00+00:00', %s)",
        (SLUG, SEED, Decimal("0.4000")),
    )
    conn.commit()


_TEST_FINDING_KEY = "answerer_dsl_candidate:oltest_learn_gate"


def _restore(conn: psycopg.Connection) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM ralph_runs WHERE project_slug = %s", (SLUG,))
    cur.execute("DELETE FROM learning_records WHERE project_slug = %s", (SLUG,))
    cur.execute("DELETE FROM run_audit_findings WHERE finding_key = %s", (_TEST_FINDING_KEY,))
    conn.commit()


def test_item2_read_completed_runs_and_learn_live() -> None:
    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision(verify)
        registry = Registry(cast(DBConnection, live))

        rows = list(registry.read_completed_runs())
        ours = [r for r in rows if r.get("project_id") == SLUG]
        assert len(ours) == 2  # both terminal rows read back from real Postgres
        statuses = {str(r["status"]) for r in ours}
        assert statuses == {"complete", "failed"}

        # Learning corpus: cost + duration derived from the real columns.
        records = {r.project_slug + ":" + r.status: r for r in learning_records(ours)}
        complete = records[f"{SLUG}:complete"]
        assert complete.cost_usd == Decimal("1.2500")
        assert complete.duration_seconds == 300.0  # 5 minutes
        corpus = render_learning_corpus(learning_records(ours))
        assert SLUG in corpus

        # The Learn pass runs over the live records (read-only audit; no registry write).
        sink: list[RunAuditReport] = []
        report = run_learn_step(
            LearnConfig(
                runs_source=lambda: completed_run_records(ours), report_sink=sink.append
            )
        )
        assert report is not None
        assert report.runs_audited == 2
        assert sink == [report]
    finally:
        _restore(verify)
        verify.close()
        live.close()


def test_item2_db_capture_round_trip_live() -> None:
    """Item 2 DB capture: the Registry persists learning_records + run_audit_findings to the real
    ol3 tables and reads the findings back; upsert is idempotent and returns NEW keys once."""
    from supervisor.learn_assembly import learning_records
    from supervisor.run_auditor import AuditFinding, FindingKind

    verify = _connect(autocommit=True)
    live = _connect()
    try:
        _provision(verify)
        registry = Registry(cast(DBConnection, live))

        # learning_records: capture from the live terminal Runs, then read them straight back.
        rows = [r for r in registry.read_completed_runs() if r.get("project_id") == SLUG]
        registry.upsert_learning_records(learning_records(rows))
        cur = verify.cursor()
        cur.execute("SELECT count(*) FROM learning_records WHERE project_slug = %s", (SLUG,))
        captured = cur.fetchone()
        assert captured is not None and int(captured[0]) == 2

        # run_audit_findings: first upsert reports the key NEW; the second reports it already seen.
        finding = AuditFinding(
            kind=FindingKind.ANSWERER_DSL_CANDIDATE,
            subject="oltest_learn_gate",
            evidence="escalated + resolved 'A' across 3 Runs",
            recommendation="add an Answerer rule pre-resolving 'oltest_learn_gate' to 'A'",
            routes_to="operator + cf-spec-writer",
        )
        first = registry.upsert_audit_findings([finding], runs_audited=3)
        second = registry.upsert_audit_findings([finding], runs_audited=4)
        assert first == [_TEST_FINDING_KEY]  # NEW on first capture (drives the one-confirm offer)
        assert second == []  # already seen → not re-surfaced (DB dedup)

        found = {row["finding_key"]: row for row in registry.read_audit_findings()}
        assert _TEST_FINDING_KEY in found
        assert found[_TEST_FINDING_KEY]["runs_audited"] == 4  # refreshed by the 2nd upsert
    finally:
        _restore(verify)
        verify.close()
        live.close()
