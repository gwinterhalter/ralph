"""Component tests for the OLB-08a RegistryPort reconcile-field extension.

Covers the OLB-08a predicate (Spec v1.3 §5.4): the additive ``RegistryPort``
extension gives the FR-009 ``orchestrator_pid`` (post-spawn) and the FR-011 /
FR-014 ``terminated_at`` + exact-decimal ``terminal_cost_usd`` a real
persistence home, and ``admission.admit_and_spawn`` wires the post-spawn pid
write — all WITHOUT changing any pre-existing ``RegistryPort`` signature.

DB-free / hermetic (gate ``olb08a-registryport-extension-substrate`` option A):
the concrete ``Registry`` is driven by the same in-memory fake connection as the
OLB-02 suite (it records the SQL it is handed and serves programmed reads), so
the reconcile/pid UPDATEs and the §5.4 status guard are proven against the actual
shipped write surface without a live branch. The LIVE Supabase-branch invocation
of ``reconcile_run`` / ``set_run_orchestrator_pid`` is exercised at C2/OLB-08.
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import pytest

from supervisor import admission
from supervisor.admission import (
    ReconciledFailure,
    RunRecord,
    SpawnResult,
    admit_and_spawn,
)
from supervisor.ports import RegistryPort, RegistryRow
from supervisor.registry import RUN_STATUSES, Registry
from supervisor.safety_gates import BlastRadiusScope


# --- A fake psycopg connection: records SQL, serves programmed read results ---
# Mirrors the OLB-02 suite's in-memory harness (test_supervisor_registry.py) so
# the reconcile-field extension is proven against the real Registry write surface.


class _FakeCursor:
    """Minimal psycopg-cursor stand-in: a context manager recording every
    ``execute(sql, params)`` on the parent connection."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: Sequence[object] = ()) -> None:
        self._conn.executed.append((query, tuple(params)))

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._conn.fetchall_result

    def fetchone(self) -> tuple[object, ...] | None:
        return self._conn.fetchone_result


class _FakeConn:
    """In-memory stand-in for an injected psycopg ``Connection``: records every
    executed ``(sql, params)`` pair and every ``commit()``."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0
        self.fetchall_result: list[tuple[object, ...]] = []
        self.fetchone_result: tuple[object, ...] | None = None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def conn() -> _FakeConn:
    """A fresh fake connection per test."""
    return _FakeConn()


@pytest.fixture
def registry(conn: _FakeConn) -> Registry:
    """The real Registry over a fresh fake connection."""
    return Registry(conn)


# --- (a) reconcile_run — FR-011 terminated_at + FR-014 exact-decimal cost ---


@pytest.mark.unit
def test_reconcile_run_persists_status_terminated_at_and_exact_decimal_cost(
    registry: Registry, conn: _FakeConn
) -> None:
    """FR-011/FR-014: reconcile_run issues one parameterised UPDATE carrying the
    terminal status, the terminated_at boundary, and terminal_cost_usd, targeting
    only the running row, and commits."""
    cost = Decimal("12.3456")
    registry.reconcile_run(
        "p1", "complete", terminated_at="2026-06-05T00:00:00+00:00",
        terminal_cost_usd=cost,
    )

    sql, params = conn.executed[0]
    assert sql.startswith("UPDATE ralph_runs SET status")
    assert "terminated_at = %s" in sql
    assert "terminal_cost_usd = %s" in sql
    assert "WHERE project_slug = %s AND status = 'running'" in sql
    assert params == ("complete", "2026-06-05T00:00:00+00:00", cost, "p1")
    assert conn.commits == 1


@pytest.mark.unit
def test_reconcile_run_binds_cost_as_exact_decimal_not_float(
    registry: Registry, conn: _FakeConn
) -> None:
    """NFR-007: terminal_cost_usd reaches the driver as an exact ``Decimal`` (the
    psycopg->NUMERIC adapter), never coerced to ``float`` — so no binary-float
    rounding can enter the money column."""
    cost = Decimal("0.1") + Decimal("0.2")  # 0.3 exactly; 0.30000000000000004 as float
    registry.reconcile_run(
        "p1", "complete", terminated_at="2026-06-05T00:00:00+00:00",
        terminal_cost_usd=cost,
    )

    _sql, params = conn.executed[0]
    bound_cost = params[2]
    assert isinstance(bound_cost, Decimal)
    assert not isinstance(bound_cost, float)
    assert bound_cost == Decimal("0.3")


@pytest.mark.unit
def test_reconcile_run_rejects_out_of_set_status_before_any_write(
    registry: Registry, conn: _FakeConn
) -> None:
    """The §5.4 CHECK-set guard (shared with update_run_status) rejects an
    out-of-set status BEFORE any write — no UPDATE, no commit."""
    assert "bogus" not in RUN_STATUSES

    with pytest.raises(ValueError):
        registry.reconcile_run(
            "p1", "bogus", terminated_at="2026-06-05T00:00:00+00:00",
            terminal_cost_usd=Decimal("0"),
        )

    assert conn.executed == []
    assert conn.commits == 0


# --- (b) set_run_orchestrator_pid — FR-009 post-spawn pid persistence ---


@pytest.mark.unit
def test_set_run_orchestrator_pid_persists_pid_against_running_row(
    registry: Registry, conn: _FakeConn
) -> None:
    """FR-009: set_run_orchestrator_pid issues one parameterised UPDATE writing
    orchestrator_pid against the Project's running Run row, and commits."""
    registry.set_run_orchestrator_pid("p1", 4321)

    sql, params = conn.executed[0]
    assert sql.startswith("UPDATE ralph_runs SET orchestrator_pid")
    assert "WHERE project_slug = %s AND status = 'running'" in sql
    assert params == (4321, "p1")
    assert conn.commits == 1


# --- (c) admit_and_spawn post-spawn pid wiring (FR-021 / §6.3) ---
# A recording RegistryPort double: records the write calls admit_and_spawn issues
# so the post-spawn pid wiring can be asserted without a concrete DB.


class _RecordingRegistry:
    """A RegistryPort double recording every write call as ``(method, args)``.
    Reads are unused on the admit_and_spawn path and return empty."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def read_candidates(self) -> Sequence[RegistryRow]:
        return []

    def read_running(self) -> Sequence[RegistryRow]:
        return []

    def set_lifecycle_state(self, project_id: str, state: str) -> None:
        self.calls.append(("set_lifecycle_state", (project_id, state)))

    def record_run(self, project_id: str, run: RegistryRow) -> None:
        self.calls.append(("record_run", (project_id, run)))

    def update_run_status(self, project_id: str, status: str) -> None:
        self.calls.append(("update_run_status", (project_id, status)))

    def reconcile_run(
        self, project_id: str, status: str, *,
        terminated_at: str, terminal_cost_usd: Decimal,
    ) -> None:
        self.calls.append(
            ("reconcile_run", (project_id, status, terminated_at, terminal_cost_usd))
        )

    def set_run_orchestrator_pid(self, project_id: str, orchestrator_pid: int) -> None:
        self.calls.append(("set_run_orchestrator_pid", (project_id, orchestrator_pid)))


class _OkSpawn:
    """A SpawnPort double whose spawn returns a configured SpawnResult."""

    def __init__(self, result: SpawnResult) -> None:
        self._result = result

    def spawn(
        self, seed_path: str, blast_radius_scope: BlastRadiusScope
    ) -> SpawnResult:
        return self._result


def _candidate() -> dict[str, object]:
    return {"project_id": "p1", "seed_path": "/seed"}


def _scope() -> BlastRadiusScope:
    return BlastRadiusScope(
        read_only_paths=frozenset(), writable_paths=frozenset({"/w"})
    )


@pytest.mark.unit
def test_admit_and_spawn_persists_orchestrator_pid_on_success() -> None:
    """FR-009/FR-021: on a successful spawn carrying a pid, admit_and_spawn calls
    set_run_orchestrator_pid with the SpawnResult.orchestrator_pid (after the
    pre-spawn record_run, which is not re-ordered)."""
    registry = _RecordingRegistry()
    spawn = _OkSpawn(SpawnResult(ok=True, orchestrator_pid=9876))

    result = admit_and_spawn(
        _candidate(), registry_port=registry, spawn_port=spawn,
        blast_radius_scope=_scope(), clock=lambda: "2026-06-05T00:00:00+00:00",
    )

    assert isinstance(result, RunRecord)
    assert result.orchestrator_pid == 9876
    assert ("set_run_orchestrator_pid", ("p1", 9876)) in registry.calls
    # FR-021 ordering preserved: record_run is issued before the pid write.
    methods = [name for name, _ in registry.calls]
    assert methods.index("record_run") < methods.index("set_run_orchestrator_pid")


@pytest.mark.unit
def test_admit_and_spawn_does_not_persist_pid_when_spawn_returns_none_pid() -> None:
    """A successful spawn with no pid (None) must NOT issue a null-pid write."""
    registry = _RecordingRegistry()
    spawn = _OkSpawn(SpawnResult(ok=True, orchestrator_pid=None))

    result = admit_and_spawn(
        _candidate(), registry_port=registry, spawn_port=spawn,
        blast_radius_scope=_scope(), clock=lambda: "2026-06-05T00:00:00+00:00",
    )

    assert isinstance(result, RunRecord)
    assert "set_run_orchestrator_pid" not in {name for name, _ in registry.calls}


@pytest.mark.unit
def test_admit_and_spawn_does_not_persist_pid_on_spawn_failure() -> None:
    """On the spawn-failure path the Run is reconciled to failed and NO pid write
    is issued (the pid persistence is strictly a success-path step)."""
    registry = _RecordingRegistry()
    spawn = _OkSpawn(SpawnResult(ok=False, detail="boom"))

    result = admit_and_spawn(
        _candidate(), registry_port=registry, spawn_port=spawn,
        blast_radius_scope=_scope(), clock=lambda: "2026-06-05T00:00:00+00:00",
    )

    assert isinstance(result, ReconciledFailure)
    assert "set_run_orchestrator_pid" not in {name for name, _ in registry.calls}
    assert ("update_run_status", ("p1", "failed")) in registry.calls


# --- (d) Seam integrity — the five pre-existing RegistryPort methods unchanged ---


@pytest.mark.unit
def test_concrete_registry_still_satisfies_the_registry_port(
    registry: Registry,
) -> None:
    """The extended concrete Registry still structurally satisfies RegistryPort —
    the additive methods did not break the OLB-01 seam."""
    assert isinstance(registry, RegistryPort)


@pytest.mark.unit
def test_preexisting_registry_port_methods_are_unchanged() -> None:
    """Seam integrity (NFR-006): the five OLB-02 RegistryPort methods retain their
    exact pre-OLB-08a signatures; OLB-08a only ADDED reconcile_run and
    set_run_orchestrator_pid."""
    import inspect

    # Parameter name+order lists are the stable seam surface (robust across the
    # PEP 563 stringized-annotation repr); any rename/add/remove on a pre-existing
    # method would change this list.
    expected_params = {
        "read_candidates": ["self"],
        "read_running": ["self"],
        "set_lifecycle_state": ["self", "project_id", "state"],
        "record_run": ["self", "project_id", "run"],
        "update_run_status": ["self", "project_id", "status"],
    }
    for name, params in expected_params.items():
        actual = list(inspect.signature(getattr(RegistryPort, name)).parameters)
        assert actual == params, name

    # The two additive methods exist on the Protocol (extension landed).
    assert hasattr(RegistryPort, "reconcile_run")
    assert hasattr(RegistryPort, "set_run_orchestrator_pid")
    # admission consumes the new pid seam (import-level wiring sanity).
    assert hasattr(admission, "admit_and_spawn")
