"""Concrete Project Registry read/write layer for the Outer Loop Supervisor (OLB-02).

Implements the OLB-01 ``RegistryPort`` Protocol (``supervisor/ports.py``) against
the extended ``projects`` table (Spec v1.3 §5.1-§5.2) and the ``ralph_runs`` Run
Registry (§5.4), reaching the ``code_factory`` Postgres on the disposable branch
via a direct psycopg driver (gate ``olb02-registry-db-access`` option A).

Sole-writer discipline (§5.5, NFR-006): this module is the ONLY write surface for
the Project Registry and Run Registry. Every lifecycle-state UPDATE passes through
``transitions.assert_legal_transition`` before it touches the database, so an
illegal §5.3 transition (FR-008) is rejected at the write boundary.

The DB connection is constructor-injected (a psycopg ``Connection``, or any object
exposing the same ``cursor()`` / ``commit()`` surface), so the component suite
substitutes an in-memory fake and runs hermetically. The live branch connection
(env var ``OL_SUPERVISOR_DB_URL``) is provisioned and exercised only at C2/OLB-08
per the seed's no-big-bang principle; ``psycopg`` is imported lazily in
:meth:`Registry.from_env` so importing this module needs no driver and no
database.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol, cast

from supervisor import transitions

if TYPE_CHECKING:
    from supervisor.ports import RegistryRow


class _DBCursor(Protocol):
    """The narrow DB-API cursor surface this layer uses (a psycopg ``Cursor``,
    or any context-manager cursor exposing it). Parameters are positional-only so
    a fake's parameter names need not match."""

    def execute(self, query: str, params: Sequence[object] = (), /) -> object: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def __enter__(self) -> _DBCursor: ...

    def __exit__(self, *exc: object) -> None: ...


class DBConnection(Protocol):
    """The narrow connection surface the Registry is injected with: a psycopg
    ``Connection`` (or any object exposing the same ``cursor()`` /
    ``commit()`` surface). Constructor-injected so the unit suite substitutes a
    fake and runs hermetically."""

    def cursor(self) -> _DBCursor: ...

    def commit(self) -> None: ...

# Env var carrying the branch session-pooler connection string. Read only by the
# live-connection factory (:meth:`Registry.from_env`); the unit suite never sets
# it (it injects a fake connection instead).
DB_URL_ENV = "OL_SUPERVISOR_DB_URL"

# The columns read back by read_candidates / read_running: the seven legacy
# ``projects`` columns plus the five supervision columns (Spec v1.3 §5.2). Named
# once as a fixed internal allowlist so the SELECT list and the row mapping cannot
# drift, and so no column name is ever interpolated from caller input.
PROJECT_COLUMNS: tuple[str, ...] = (
    "project_id",
    "display_name",
    "folder_path",
    "kind",
    "status",
    "lifecycle_state",
    "priority",
    "blast_radius_scope",
    "attention_debt",
    "heartbeat_workstream_id",
)

# The ``ralph_runs`` columns a spawn row may carry (Spec v1.3 §5.4). ``project_slug``
# is supplied separately from the ``project_id`` argument (FR-010 soft reference);
# the remaining columns are taken from the run mapping when present. A fixed
# allowlist so a caller key can never inject a column name into the INSERT.
RALPH_RUNS_INSERT_COLUMNS: tuple[str, ...] = (
    "run_id",
    "seed_path",
    "orchestrator_pid",
    "status",
    "idempotency_key",
    "spawned_at",
    "terminal_cost_usd",
    "metadata",
)

# The ``ralph_runs.status`` CHECK set (Spec v1.3 §5.4). update_run_status validates
# against this before the UPDATE so an out-of-set status never reaches the database.
RUN_STATUSES: frozenset[str] = frozenset(
    {"running", "complete", "budget_exhausted", "failed", "halted"}
)


class Registry:
    """Concrete psycopg-backed :class:`~supervisor.ports.RegistryPort`.

    Reads the extended ``projects`` table and writes ``projects`` lifecycle state
    and ``ralph_runs`` rows. The sole write surface for both registries (§5.5,
    NFR-006); the supervision cycle host issues no write of its own.
    """

    def __init__(self, connection: DBConnection) -> None:
        # A psycopg Connection, or any test double exposing the same
        # ``cursor()`` (context-manager) / ``commit()`` surface. Injected so the
        # unit suite runs without a live branch.
        self._conn = connection

    @classmethod
    def from_env(cls, env_var: str = DB_URL_ENV) -> Registry:
        """Build a Registry against the live branch connection named by ``env_var``.

        ``psycopg`` is imported lazily HERE so importing this module — and the
        entire unit suite — needs no driver and no database (the live branch is
        exercised only at C2/OLB-08). Raises ``RuntimeError`` if the env var is
        unset.
        """
        dsn = os.environ.get(env_var)
        if not dsn:
            raise RuntimeError(
                f"{env_var} is not set; the live Project Registry connection is "
                f"provisioned at the C2/OLB-08 checkpoint preflight "
                f"(gate olb02-registry-db-access option A)."
            )
        # lazy: keeps this module's import and the unit suite DB-free. psycopg is
        # a declared dependency provisioned at the C2/OLB-08 preflight, so it is
        # absent from the hermetic type-check / unit env.
        import psycopg  # type: ignore[import-not-found]

        return cls(psycopg.connect(dsn))

    # --- Reads (Spec v1.3 §5.2 / §6 FR-015) ---

    def read_candidates(self) -> Sequence[RegistryRow]:
        """Return Projects in the ``candidate`` lifecycle state (Spec §6 FR-015)."""
        return self._select_projects_by_state("candidate")

    def read_running(self) -> Sequence[RegistryRow]:
        """Return Projects in the ``running`` lifecycle state (Spec §5.3/§5.4)."""
        return self._select_projects_by_state("running")

    def _select_projects_by_state(self, lifecycle_state: str) -> list[RegistryRow]:
        # Column list is the fixed internal PROJECT_COLUMNS allowlist (never caller
        # input); the lifecycle_state value is parameterised, so this is not an
        # injection vector (bandit B608 false positive — suppressed inline).
        cols = ", ".join(PROJECT_COLUMNS)
        sql = f"SELECT {cols} FROM projects WHERE lifecycle_state = %s"  # nosec B608
        with self._conn.cursor() as cur:
            cur.execute(sql, (lifecycle_state,))
            rows = cur.fetchall()
        mapped: list[RegistryRow] = [
            dict(zip(PROJECT_COLUMNS, row)) for row in rows
        ]
        return mapped

    # --- Writes (Spec v1.3 §5.2-§5.5; sole write surface, NFR-006) ---

    def set_lifecycle_state(self, project_id: str, state: str) -> None:
        """Transition a Project's lifecycle state (Spec §5.2 FR-001, §5.3 FR-008).

        Reads the Project's current state and rejects an illegal §5.3 transition
        BEFORE the UPDATE — so an illegal transition never reaches the database
        (FR-008 enforced at the write boundary).
        """
        current = self._current_lifecycle_state(project_id)
        transitions.assert_legal_transition(current, state)
        self._execute_write(
            "UPDATE projects SET lifecycle_state = %s, updated_at = now() "
            "WHERE project_id = %s",
            (state, project_id),
        )

    def record_run(self, project_id: str, run: RegistryRow) -> None:
        """Insert a Run Registry row at spawn (Spec §5.4 FR-009).

        ``project_id`` is written as the soft ``project_slug`` reference (FR-010);
        the remaining columns are taken from ``run`` against the fixed
        RALPH_RUNS_INSERT_COLUMNS allowlist.
        """
        columns = ["project_slug"]
        values: list[object] = [project_id]
        for col in RALPH_RUNS_INSERT_COLUMNS:
            if col in run:
                columns.append(col)
                values.append(run[col])
        placeholders = ", ".join(["%s"] * len(columns))
        # Column names come from the fixed allowlist above (never caller keys); all
        # values are parameterised, so this is not an injection vector (bandit B608
        # false positive — suppressed inline).
        col_list = ", ".join(columns)
        sql = f"INSERT INTO ralph_runs ({col_list}) VALUES ({placeholders})"  # nosec B608
        self._execute_write(sql, tuple(values))

    def update_run_status(self, project_id: str, status: str) -> None:
        """Reconcile the active Run for a Project to a terminal status
        (Spec §5.4 FR-011/FR-012).

        Validates ``status`` against the §5.4 CHECK set before the UPDATE, and
        targets only the Project's currently-``running`` Run row.
        """
        if status not in RUN_STATUSES:
            raise ValueError(
                f"illegal ralph_runs status {status!r} "
                f"(Spec v1.3 §5.4); legal: {sorted(RUN_STATUSES)}"
            )
        self._execute_write(
            "UPDATE ralph_runs SET status = %s, updated_at = now() "
            "WHERE project_slug = %s AND status = 'running'",
            (status, project_id),
        )

    def _execute_write(self, sql: str, params: Sequence[object]) -> None:
        """Execute a single parameterised write and commit it. The one place the
        cursor/commit dance lives, so every write path commits consistently."""
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
        self._conn.commit()

    def _current_lifecycle_state(self, project_id: str) -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT lifecycle_state FROM projects WHERE project_id = %s",
                (project_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"no projects row for project_id {project_id!r}")
        # lifecycle_state is a NOT-NULL text column (Spec v1.3 §5.2 FR-001).
        return cast(str, row[0])
