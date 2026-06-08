"""ABS on-ramp — LIVE upsert_project provisioning round-trip (dev branch)."""
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
        f"ABS on-ramp live checkpoint requires {DB_URL_ENV} pointing at the disposable branch "
        f"{BRANCH_REF} (production ref {PRODUCTION_REF} must be absent)."
    ),
)
pytestmark = [pytest.mark.integration, requires_branch]


def _conn() -> psycopg.Connection:
    return psycopg.connect(_DSN)


def test_upsert_project_provisions_chain_live() -> None:
    live = _conn()
    verify = _conn()
    verify.autocommit = True
    try:
        verify.cursor().execute("DELETE FROM projects WHERE project_id LIKE 'oltest_abs_%%'")
        registry = Registry(cast(DBConnection, live))

        assert registry.upsert_project(
            "oltest_abs_a", folder_path="oltest_abs_a", priority=30, depends_on=[]
        ) is True
        assert registry.upsert_project(
            "oltest_abs_b", folder_path="oltest_abs_b", priority=20, depends_on=["oltest_abs_a"]
        ) is True
        # Idempotent / non-clobbering: re-provisioning an existing project is a no-op.
        assert registry.upsert_project(
            "oltest_abs_a", folder_path="oltest_abs_a", priority=30, depends_on=[]
        ) is False

        rows = {r["project_id"]: r for r in registry.read_all_projects() if str(r["project_id"]).startswith("oltest_abs_")}
        assert set(rows) == {"oltest_abs_a", "oltest_abs_b"}
        assert rows["oltest_abs_b"]["depends_on"] == ["oltest_abs_a"]  # the Item 1 gate edge
        assert rows["oltest_abs_b"]["lifecycle_state"] == "candidate"
    finally:
        verify.cursor().execute("DELETE FROM projects WHERE project_id LIKE 'oltest_abs_%%'")
        live.close()
        verify.close()
