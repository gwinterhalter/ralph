"""D7 — the OL-2 migration DDL covers exactly what preflight asserts + the code's enums.

A DB-free consistency check: the migration .sql must declare every column / constraint
/ index the supervisor depends on, so a fresh apply yields a substrate that passes
supervisor/preflight.py. (Applying the DDL is live-only; this ties the authored DDL
to the asserted shape without a database.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor.preflight import (
    REQUIRED_ACTIVE_RUN_INDEX,
    REQUIRED_PROJECT_COLUMNS,
    REQUIRED_RALPH_RUNS_COLUMNS,
    REQUIRED_RALPH_RUNS_NOT_NULL,
)
from supervisor.registry import RUN_STATUSES
from supervisor.transitions import LIFECYCLE_STATES

pytestmark = pytest.mark.unit

_DDL = (
    Path(__file__).resolve().parent.parent / "migrations" / "ol2_supervision_schema.sql"
).read_text(encoding="utf-8")


# `project_id` is the pre-existing legacy PK the additive migration assumes (it does
# not re-declare it); every OTHER required projects column is supervision-added here.
_LEGACY_PROJECT_COLUMNS = {"project_id"}


def test_migration_declares_all_added_project_columns() -> None:
    for col in REQUIRED_PROJECT_COLUMNS - _LEGACY_PROJECT_COLUMNS:
        assert col in _DDL, f"migration missing projects supervision column {col}"


def test_migration_declares_all_required_ralph_runs_columns() -> None:
    for col in REQUIRED_RALPH_RUNS_COLUMNS:
        assert col in _DDL, f"migration missing ralph_runs column {col}"


def test_seed_path_is_not_null() -> None:
    # The iter-0017 production-shape regression: seed_path MUST be NOT NULL.
    for col in REQUIRED_RALPH_RUNS_NOT_NULL:
        assert col == "seed_path"
    assert "seed_path" in _DDL
    # the seed_path declaration line carries NOT NULL
    seed_line = next(line for line in _DDL.splitlines() if "seed_path" in line and "text" in line)
    assert "NOT NULL" in seed_line


def test_active_run_unique_index_present_and_partial() -> None:
    assert REQUIRED_ACTIVE_RUN_INDEX in _DDL
    assert "WHERE status = 'running'" in _DDL  # FR-007 partial uniqueness


def test_status_check_covers_run_statuses() -> None:
    for status in RUN_STATUSES:
        assert f"'{status}'" in _DDL, f"ralph_runs.status CHECK missing {status}"


def test_lifecycle_check_covers_all_states() -> None:
    for state in LIFECYCLE_STATES:
        assert f"'{state}'" in _DDL, f"lifecycle_state CHECK missing {state}"


def test_migration_is_additive_and_idempotent() -> None:
    # Additive: no destructive verbs against the existing projects table.
    upper = _DDL.upper()
    assert "DROP TABLE" not in upper
    assert "DROP COLUMN" not in upper
    # Idempotent guards.
    assert "ADD COLUMN IF NOT EXISTS" in upper
    assert "CREATE TABLE IF NOT EXISTS" in upper
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in upper
