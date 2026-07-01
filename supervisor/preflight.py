"""Startup preflight schema/env gate (robustness T3#5).

The build hit failures *deep inside* iterations that a startup check would have
surfaced immediately and legibly — most notably ``ralph_runs.seed_path NOT NULL``
violated mid-run (a code/schema mismatch), and DSN/connectivity problems. This
module asserts, before the supervisor runs, that the live substrate matches the
shape the code depends on: the required ``projects`` / ``ralph_runs`` columns, the
``seed_path NOT NULL`` constraint, the ``uq_ralph_runs_active_per_project`` active-run
index, and basic connectivity. A drift returns a structured failure list so the
operator gets a fast, named error instead of a deep mid-iteration traceback.

The checker is split so the decision logic is hermetically testable: ``introspect``
is the thin psycopg adapter that reads ``information_schema`` / ``pg_indexes`` into a
:class:`SchemaSnapshot`, and ``evaluate_preflight`` is the pure verdict over a
snapshot. ``run_preflight`` composes them; the ``__main__`` CLI wires it to a live
connection from ``PROD_DB_URL`` and exits non-zero on drift.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from supervisor.registry import DB_URL_ENV, DBConnection

#: Columns the supervisor reads off a ``projects`` row (subset that must exist).
REQUIRED_PROJECT_COLUMNS: frozenset[str] = frozenset(
    {
        "project_id",
        "lifecycle_state",
        "folder_path",
        "priority",
        "attention_debt",
        "depends_on",  # Item 1 cross-initiative dependency gating
    }
)
#: Columns the supervisor reads/writes on a ``ralph_runs`` row (must exist).
REQUIRED_RALPH_RUNS_COLUMNS: frozenset[str] = frozenset(
    {
        "project_slug",
        "seed_path",
        "status",
        "orchestrator_pid",
        "metadata",  # FR-013 recorded half lives in metadata.pid_start_time
        "spawned_at",
        "terminated_at",
        "terminal_cost_usd",
    }
)
#: ``ralph_runs`` columns that MUST be NOT NULL (the iter-0017 production shape).
REQUIRED_RALPH_RUNS_NOT_NULL: frozenset[str] = frozenset({"seed_path"})
#: The FR-007 active-run partial unique index.
REQUIRED_ACTIVE_RUN_INDEX = "uq_ralph_runs_active_per_project"


@dataclass(frozen=True)
class SchemaSnapshot:
    """What preflight observed of the live substrate."""

    can_connect: bool
    project_columns: frozenset[str]
    ralph_runs_columns: frozenset[str]
    ralph_runs_not_null: frozenset[str]
    indexes: frozenset[str]


@dataclass(frozen=True)
class PreflightResult:
    """The preflight verdict — ``ok`` with the list of named failures."""

    ok: bool
    failures: tuple[str, ...] = field(default=())


def evaluate_preflight(snapshot: SchemaSnapshot) -> PreflightResult:
    """Pure verdict: the substrate matches the shape the supervisor depends on."""
    failures: list[str] = []

    if not snapshot.can_connect:
        failures.append(f"cannot connect to the registry DB ({DB_URL_ENV})")

    missing_proj = REQUIRED_PROJECT_COLUMNS - snapshot.project_columns
    if missing_proj:
        failures.append(f"projects missing columns: {sorted(missing_proj)}")

    missing_runs = REQUIRED_RALPH_RUNS_COLUMNS - snapshot.ralph_runs_columns
    if missing_runs:
        failures.append(f"ralph_runs missing columns: {sorted(missing_runs)}")

    missing_not_null = REQUIRED_RALPH_RUNS_NOT_NULL - snapshot.ralph_runs_not_null
    if missing_not_null:
        failures.append(
            f"ralph_runs columns must be NOT NULL but are nullable: {sorted(missing_not_null)}"
        )

    if REQUIRED_ACTIVE_RUN_INDEX not in snapshot.indexes:
        failures.append(
            f"missing active-run unique index {REQUIRED_ACTIVE_RUN_INDEX!r} (FR-007)"
        )

    return PreflightResult(ok=not failures, failures=tuple(failures))


def introspect(conn: DBConnection) -> SchemaSnapshot:  # pragma: no cover - thin DB adapter
    """Read the live schema into a :class:`SchemaSnapshot` (psycopg adapter)."""

    def _columns(table: str) -> list[tuple[str, str]]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = %s",
                (table,),
            )
            return [(str(r[0]), str(r[1])) for r in cur.fetchall()]

    proj = _columns("projects")
    runs = _columns("ralph_runs")
    with conn.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", ("ralph_runs",))
        indexes = frozenset(str(r[0]) for r in cur.fetchall())

    return SchemaSnapshot(
        can_connect=True,
        project_columns=frozenset(name for name, _ in proj),
        ralph_runs_columns=frozenset(name for name, _ in runs),
        ralph_runs_not_null=frozenset(name for name, nullable in runs if nullable == "NO"),
        indexes=indexes,
    )


def run_preflight(conn: DBConnection) -> PreflightResult:  # pragma: no cover - thin DB adapter
    """Introspect the live connection and return the verdict."""
    return evaluate_preflight(introspect(conn))


def _main() -> int:  # pragma: no cover - CLI entrypoint
    dsn = os.environ.get(DB_URL_ENV)
    if not dsn:
        print(f"preflight: {DB_URL_ENV} is not set — cannot reach the registry DB.")
        return 1
    try:
        import psycopg
    except ImportError:
        print("preflight: psycopg not installed in this environment.")
        return 1
    from typing import cast

    try:
        with psycopg.connect(dsn) as conn:
            result = run_preflight(cast(DBConnection, conn))
    except Exception as exc:  # noqa: BLE001 - report any connect/introspection failure
        print(f"preflight: FAILED to introspect the registry DB: {exc}")
        return 1
    if result.ok:
        print("preflight: OK — registry schema matches the expected shape.")
        return 0
    print("preflight: FAILED — substrate drift:")
    for failure in result.failures:
        print(f"  - {failure}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())


__all__ = [
    "SchemaSnapshot",
    "PreflightResult",
    "evaluate_preflight",
    "introspect",
    "run_preflight",
    "REQUIRED_PROJECT_COLUMNS",
    "REQUIRED_RALPH_RUNS_COLUMNS",
    "REQUIRED_RALPH_RUNS_NOT_NULL",
    "REQUIRED_ACTIVE_RUN_INDEX",
]
