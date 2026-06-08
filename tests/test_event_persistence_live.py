"""Fleet event persistence — LIVE round-trip against the ol5 events table (dev branch)."""
from __future__ import annotations

import os
from typing import cast

import psycopg
import pytest

from supervisor.registry import DBConnection, Registry

DB_URL_ENV = "OL_SUPERVISOR_DB_URL"
BRANCH_REF = "jmjncijbbakuzndqhssw"
PRODUCTION_REF = "eybdbshxswutgaaylpol"
_DSN = os.environ.get(DB_URL_ENV, "")
_ON_BRANCH = bool(_DSN) and BRANCH_REF in _DSN and PRODUCTION_REF not in _DSN

requires_branch = pytest.mark.skipif(
    not _ON_BRANCH,
    reason=(
        f"event-persistence live checkpoint requires {DB_URL_ENV} pointing at the disposable "
        f"branch {BRANCH_REF} (production ref {PRODUCTION_REF} must be absent)."
    ),
)
pytestmark = [pytest.mark.integration, requires_branch]


def _conn() -> psycopg.Connection:
    return psycopg.connect(_DSN)


def test_event_persistence_round_trip_live() -> None:
    live = _conn()
    verify = _conn()
    verify.autocommit = True
    try:
        registry = Registry(cast(DBConnection, live))
        uuid1 = "0a000000-0000-4000-8000-000000000001"
        uuid2 = "0a000000-0000-4000-8000-000000000002"
        # The canonical events table has several NOT NULL columns — a real events.jsonl carries
        # all 9 §4.1 required keys; supply them here.
        base = {
            "schema_version": 1,
            "project_id": "oltest_ev",
            "initiative_slug": "oltest_ev",
            "iteration_index": 1,
            "role": "gate",
            "ts_utc": "2026-06-07T10:00:00.000Z",
        }
        events = [
            {
                **base,
                "event_uuid": uuid1,
                "event_type": "gate_fire",
                "payload": {"gate_id": "g1", "cls": "gate_human"},
                "subject_id": "g1",
                "subject_kind": "gate",
            },
            {
                **base,
                "event_uuid": uuid2,
                "role": "executor",
                "event_type": "llm_call",
                "ts_utc": "2026-06-07T10:01:00.000Z",
                "payload": {"cost_usd": "1.25"},
            },
        ]
        assert registry.upsert_events(events) == 2  # both new
        assert registry.upsert_events(events) == 0  # idempotent re-ingest

        rows = registry.read_events_db(project_id="oltest_ev", event_type="gate_fire", limit=10)
        assert len(rows) == 1
        assert str(rows[0]["event_uuid"]) == uuid1
        assert rows[0]["payload"]["gate_id"] == "g1"  # jsonb round-trips to a dict
    finally:
        cur = verify.cursor()
        cur.execute("DELETE FROM events WHERE project_id = %s", ("oltest_ev",))
        live.close()
        verify.close()
