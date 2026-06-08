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
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, cast

from supervisor import transitions
from supervisor.learn_assembly import LearningRecord
from supervisor.run_auditor import AuditFinding, finding_key

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
    "depends_on",
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
    and ``ralph_runs`` rows. The sole *runtime* write surface for both registries
    (§5.5, NFR-006), conforming to the cf-ralph-run-tracker discipline (the §5.5
    sole write authority for ``ralph_runs``); the supervision cycle host issues
    no write of its own.
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
        # absent from the hermetic type-check / unit env (pyproject [tool.mypy]
        # ignore_missing_imports handles the absent case; the cast keeps the gate
        # strict-clean in a psycopg-PRESENT env too — FUP-0856).
        import psycopg

        return cls(cast("DBConnection", psycopg.connect(dsn)))

    # --- Reads (Spec v1.3 §5.2 / §6 FR-015) ---

    def read_candidates(self) -> Sequence[RegistryRow]:
        """Return Projects in the ``candidate`` lifecycle state (Spec §6 FR-015)."""
        return self._select_projects_by_state("candidate")

    def read_running(self) -> Sequence[RegistryRow]:
        """Return Projects in the ``running`` lifecycle state (Spec §5.3/§5.4)."""
        return self._select_projects_by_state("running")

    def read_admitted(self) -> Sequence[RegistryRow]:
        """Return Projects in the ``admitted`` lifecycle state (Spec §6 FR-019).

        The FR-019 ceiling-hold parks a Candidate in ``admitted`` (spawn deferred until
        headroom). These must be fed back to the Schedule step so a held Project is
        dispatched once a slot frees — otherwise an admitted Project is orphaned (it is
        in neither ``read_candidates`` nor ``read_running``). Additive read, NOT part of
        the ``RegistryPort`` Protocol (no test-double ripple); the Schedule wiring
        consumes it via the injected ``admitted_source``."""
        return self._select_projects_by_state("admitted")

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

    def read_active_runs(self) -> Sequence[RegistryRow]:
        """Return the ``ralph_runs`` rows currently ``running`` (robustness T1#1).

        The §4.4(1) Reconcile step's input. Each row carries ``project_id`` (the
        FR-010 soft ``project_slug`` reference, so it keys straight into
        ``reconcile_run`` / ``set_lifecycle_state``), the ``orchestrator_pid`` to
        probe for liveness, the ``spawned_at`` progress fallback (coerced to an ISO
        string), the last-known ``terminal_cost_usd``, ``seed_path`` — the production
        completion probe resolves the run's terminal artifacts (the §13.1
        INITIATIVE_COMPLETE signal + spend ledger) off the seed's sibling ``state/``
        dir, so a clean completion is reconciled ``complete`` rather than mis-reaped as
        a stall — and ``pid_start_time``, extracted from ``metadata.pid_start_time`` (the
        FR-013 recorded half; the canonical ``ralph_runs`` convention shared with the
        Trigger Service FR-005) so the re-attach pass can disambiguate pid reuse.
        Additive read — NOT part of the ``RegistryPort`` Protocol, so it adds no
        test-double conformance ripple; the Reconcile wiring consumes it via the injected
        ``active_runs_source``.
        """
        cols = (
            "project_slug",
            "run_id",
            "orchestrator_pid",
            "metadata",
            "spawned_at",
            "terminal_cost_usd",
            "seed_path",
        )
        col_list = ", ".join(cols)
        # Column names are the fixed allowlist above (never caller input); the status
        # value is parameterised — not an injection vector (bandit B608 suppressed).
        sql = f"SELECT {col_list} FROM ralph_runs WHERE status = %s"  # nosec B608
        with self._conn.cursor() as cur:
            cur.execute(sql, ("running",))
            rows = cur.fetchall()
        mapped: list[RegistryRow] = []
        for row in rows:
            record: dict[str, object] = dict(zip(cols, row))
            record["project_id"] = record.pop("project_slug")
            spawned = record.get("spawned_at")
            iso = getattr(spawned, "isoformat", None)
            if callable(iso):
                record["spawned_at"] = iso()
            # FR-013: surface metadata.pid_start_time as a flat key for the re-attach
            # pass (psycopg adapts jsonb to a dict; absent/typeless → None).
            meta = record.pop("metadata", None)
            record["pid_start_time"] = (
                meta.get("pid_start_time") if isinstance(meta, dict) else None
            )
            mapped.append(record)
        return mapped

    def read_completed_project_ids(self) -> frozenset[str]:
        """Return the ``project_id``s of every Project in the ``complete`` lifecycle state
        (Item 1 cross-initiative dependency gating).

        The set of finished prerequisites the §6 admission dependency precondition checks a
        Candidate's ``depends_on`` against: a Candidate is held (left ``candidate``) while any
        listed prerequisite is absent from this set. Concrete-only — deliberately NOT on the
        ``RegistryPort`` Protocol (no test-double ripple); the Schedule wiring consumes it via an
        injected ``completed_project_ids`` callable. The ``complete`` value is a SQL literal here,
        never caller input — no injection vector."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT project_id FROM projects WHERE lifecycle_state = %s",
                ("complete",),
            )
            rows = cur.fetchall()
        return frozenset(str(row[0]) for row in rows)

    def read_completed_runs(self) -> Sequence[RegistryRow]:
        """Return the terminal (``complete`` / ``failed``) ``ralph_runs`` rows (Item 2 Learn).

        The §4.4(6) Learn step's live source: each row carries ``run_id``, ``project_id`` (the
        FR-010 soft ``project_slug``), the terminal ``status``, the summed ``terminal_cost_usd``
        (FR-014), the ``spawned_at`` / ``terminated_at`` boundaries (coerced to ISO strings) for
        duration, and ``metadata``. The ``supervisor.learn_assembly`` adapter maps these into the
        Run-Auditor's :class:`~supervisor.run_auditor.RunRecord` facts + the cost/duration learning
        corpus. Additive read — NOT part of the ``RegistryPort`` Protocol (no test-double ripple);
        the Learn wiring consumes it via the injected ``runs_source``. The status values are SQL
        literals here, never caller input — no injection vector."""
        cols = (
            "run_id",
            "project_slug",
            "status",
            "terminal_cost_usd",
            "spawned_at",
            "terminated_at",
            "metadata",
        )
        col_list = ", ".join(cols)
        sql = (  # nosec B608 — column names are the fixed allowlist above; status is literal
            f"SELECT {col_list} FROM ralph_runs "
            "WHERE status IN ('complete', 'failed')"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        mapped: list[RegistryRow] = []
        for row in rows:
            record: dict[str, object] = dict(zip(cols, row))
            record["project_id"] = record.pop("project_slug")
            for ts_key in ("spawned_at", "terminated_at"):
                value = record.get(ts_key)
                iso = getattr(value, "isoformat", None)
                if callable(iso):
                    record[ts_key] = iso()
            mapped.append(record)
        return mapped

    def read_cumulative_spend_usd(self) -> Decimal:
        """Fleet cumulative spend — the sum of recorded ``terminal_cost_usd`` across all
        Runs (FR-014), as an exact ``Decimal`` (NFR-007).

        The figure the opt-in emergency spend backstop (T3#6) compares to its hard
        ceiling. Concrete-only — NOT on the ``RegistryPort`` Protocol (no test-double
        ripple); the production ``main`` loop consumes it when the backstop is configured.
        ``COALESCE(..., 0)`` so an empty table reads ``Decimal('0')``, never NULL."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(terminal_cost_usd), 0) FROM ralph_runs")
            row = cur.fetchone()
        raw = row[0] if row else 0
        return raw if isinstance(raw, Decimal) else Decimal(str(raw))

    # --- Learning capture (Item 2 DB capture; ol3 tables) ---
    # The §4.4(6) Learn step's outputs persisted to queryable tables. These are the auditor's OWN
    # outputs (surfacing), NOT a mutation of any audited artifact — the Run-Auditor stays read-only
    # (FR-053). Concrete-only — NOT on the RegistryPort Protocol (no test-double ripple); the
    # production Learn wiring consumes them. Column names are SQL literals; values parameterised.

    def upsert_learning_records(self, records: "Sequence[LearningRecord]") -> None:
        """UPSERT the per-completed-Run cost/duration/status corpus (keyed by run_id; idempotent)."""
        for rec in records:
            self._execute_write(
                "INSERT INTO learning_records "
                "(run_id, project_slug, status, cost_usd, duration_seconds) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET project_slug = EXCLUDED.project_slug, "
                "status = EXCLUDED.status, cost_usd = EXCLUDED.cost_usd, "
                "duration_seconds = EXCLUDED.duration_seconds, updated_at = now()",
                (rec.run_id, rec.project_slug, rec.status, rec.cost_usd, rec.duration_seconds),
            )

    def upsert_audit_findings(
        self, findings: "Sequence[AuditFinding]", *, runs_audited: int
    ) -> list[str]:
        """UPSERT the Run-Auditor findings (keyed by finding_key) and return the NEW finding_keys.

        A finding_key absent from the table before this pass is NEW — returned so the auto-feedback
        bridge surfaces it to the operator exactly once. Recurring findings refresh
        evidence/recommendation/runs_audited + last_seen_at; first_seen_at is preserved."""
        keys = [finding_key(f) for f in findings]
        if not keys:
            return []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT finding_key FROM run_audit_findings WHERE finding_key = ANY(%s)", (keys,)
            )
            existing = {str(r[0]) for r in cur.fetchall()}
        for finding, key in zip(findings, keys):
            binding_class = (
                finding.binding_class.value if finding.binding_class is not None else None
            )
            self._execute_write(
                "INSERT INTO run_audit_findings (finding_key, kind, subject, binding_class, "
                "evidence, recommendation, routes_to, runs_audited) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (finding_key) DO UPDATE SET evidence = EXCLUDED.evidence, "
                "recommendation = EXCLUDED.recommendation, runs_audited = EXCLUDED.runs_audited, "
                "last_seen_at = now()",
                (
                    key,
                    finding.kind.value,
                    finding.subject,
                    binding_class,
                    finding.evidence,
                    finding.recommendation,
                    finding.routes_to,
                    runs_audited,
                ),
            )
        return [k for k in keys if k not in existing]

    def read_audit_findings(self) -> Sequence[RegistryRow]:
        """Return all persisted Run-Auditor findings, most-recently-seen first (control-panel read)."""
        cols = (
            "finding_key",
            "kind",
            "subject",
            "binding_class",
            "evidence",
            "recommendation",
            "routes_to",
            "runs_audited",
        )
        col_list = ", ".join(cols)
        sql = (  # nosec B608 — column names are the fixed allowlist above; no caller input
            f"SELECT {col_list} FROM run_audit_findings ORDER BY last_seen_at DESC"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    # --- Writes (Spec v1.3 §5.2-§5.5; sole write surface, NFR-006) ---

    def set_lifecycle_state(self, project_id: str, state: str) -> None:
        """Transition a Project's lifecycle state (Spec §5.2 FR-001, §5.3 FR-008).

        Reads the Project's current state and rejects an illegal §5.3 transition
        BEFORE the UPDATE — so an illegal transition never reaches the database
        (FR-008 enforced at the write boundary).

        Re-asserting the state the Project is already in is an idempotent no-op (not a
        transition), so the FR-019 admit path is safely re-entrant: spawning a Project
        the ceiling-hold already moved to ``admitted`` re-issues ``set(admitted)`` and
        must not trip the (correctly illegal) ``admitted -> admitted`` guard.
        """
        current = self._current_lifecycle_state(project_id)
        if current == state:
            return
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
        self._assert_run_status_legal(status)
        self._execute_write(
            "UPDATE ralph_runs SET status = %s, updated_at = now() "
            "WHERE project_slug = %s AND status = 'running'",
            (status, project_id),
        )

    def reconcile_run(
        self,
        project_id: str,
        status: str,
        *,
        terminated_at: str,
        terminal_cost_usd: Decimal,
    ) -> None:
        """Terminal reconciliation of the active Run for a Project
        (Spec v1.3 §5.4 FR-011 + FR-014).

        Persists the terminal ``status``, the ``terminated_at`` boundary, and the
        summed ``terminal_cost_usd`` in one UPDATE. Validates ``status`` against
        the §5.4 CHECK set before the write (same guard as
        :meth:`update_run_status`), and targets only the Project's
        currently-``running`` Run row. ``terminal_cost_usd`` is bound as a
        ``Decimal`` so psycopg adapts it to NUMERIC with no float rounding
        (NFR-007 exact-decimal money). The column names (``terminated_at`` /
        ``terminal_cost_usd``) are SQL literals here, never caller keys, and all
        values are parameterised — no injection vector.
        """
        self._assert_run_status_legal(status)
        self._execute_write(
            "UPDATE ralph_runs SET status = %s, terminated_at = %s, "
            "terminal_cost_usd = %s, updated_at = now() "
            "WHERE project_slug = %s AND status = 'running'",
            (status, terminated_at, terminal_cost_usd, project_id),
        )

    def set_run_orchestrator_pid(
        self, project_id: str, orchestrator_pid: int
    ) -> None:
        """Persist the spawned orchestrator pid on the active Run row after a
        successful spawn (Spec v1.3 §5.4 FR-009 / §6.3 active boundary).

        Targets only the Project's currently-``running`` Run row; the pid is
        parameterised and the column name is a SQL literal (no injection vector).
        Routed through :meth:`_execute_write` so it commits like every other
        write path.
        """
        self._execute_write(
            "UPDATE ralph_runs SET orchestrator_pid = %s, updated_at = now() "
            "WHERE project_slug = %s AND status = 'running'",
            (orchestrator_pid, project_id),
        )

    def record_pid_start_time(self, project_id: str, start_time: str) -> None:
        """Persist the spawned orchestrator's OS start-time into the active Run's
        ``metadata.pid_start_time`` (FR-013 recorded half).

        The re-attach pass compares this recorded value against the live OS start-time
        of the recorded pid to disambiguate pid reuse after a Supervisor restart. Stored
        under the canonical ``ralph_runs`` ``metadata.pid_start_time`` key (the convention
        the table's design reserves, shared with the Trigger Service FR-005) — NOT a
        dedicated column — so the shared table keeps one home for the fact. Written
        post-spawn alongside :meth:`set_run_orchestrator_pid`, merged via ``||`` so any
        other metadata keys are preserved. Concrete-only — deliberately NOT on the
        ``RegistryPort`` Protocol, so it adds no test-double conformance ripple; the
        admission terminal step consumes it via the injected ``record_start_time``
        callable. Targets only the Project's currently-``running`` Run row; the value is
        parameterised and the JSON key/column names are SQL literals (no injection
        vector)."""
        self._execute_write(
            "UPDATE ralph_runs SET "
            "metadata = COALESCE(metadata, '{}'::jsonb) "
            "|| jsonb_build_object('pid_start_time', %s::text), "
            "updated_at = now() "
            "WHERE project_slug = %s AND status = 'running'",
            (start_time, project_id),
        )

    @staticmethod
    def _assert_run_status_legal(status: str) -> None:
        """Reject an out-of-§5.4-CHECK-set Run status before any write — the one
        guard shared by :meth:`update_run_status` and :meth:`reconcile_run`."""
        if status not in RUN_STATUSES:
            raise ValueError(
                f"illegal ralph_runs status {status!r} "
                f"(Spec v1.3 §5.4); legal: {sorted(RUN_STATUSES)}"
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
