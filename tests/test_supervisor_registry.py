"""Component tests for the OLB-02 Project Registry read/write layer.

Covers the OLB-02 predicate (Spec v1.3 §5.1-§5.5): the concrete
``RegistryPort`` implementation (``supervisor/registry.py``) reads/writes the
extended ``projects`` table and the ``ralph_runs`` Run Registry, FR-008
lifecycle-transition legality is enforced at the ``set_lifecycle_state`` write
boundary (legal accepted, illegal rejected), and all Registry / Run-Registry
writes are confined to the Supervisor's registry layer (NFR-006 sole-writer).

DB-free / hermetic (gate ``olb02-registry-db-access`` option A): the §5.3
legality is tested directly on the pure ``transitions`` module, and the real
``Registry`` is driven by an in-memory fake connection that records the SQL it
is handed — proving the adapter's read mapping and write-boundary enforcement
without a live branch (the live branch is exercised only at C2/OLB-08). This
realises the session plan §3(b) round-trip + write-boundary intent against the
actual shipped write surface rather than a parallel fake.
"""
from __future__ import annotations

from collections.abc import Sequence

import pytest

from supervisor import transitions
from supervisor.ports import RegistryPort
from supervisor.registry import (
    PROJECT_COLUMNS,
    RUN_STATUSES,
    Registry,
)
from supervisor.transitions import IllegalTransitionError

# Every legal §5.3 edge as (src, dst), derived from the single source of truth so
# the parametrization cannot drift from the module under test.
LEGAL_EDGES = [
    (src, dst)
    for src, targets in transitions.LEGAL_TRANSITIONS.items()
    for dst in sorted(targets)
]


# --- A fake psycopg connection: records SQL, serves programmed read results ---


class _FakeCursor:
    """Minimal psycopg-cursor stand-in: a context manager exposing
    ``execute`` / ``fetchall`` / ``fetchone`` over the parent connection's
    programmed results and recorded statements."""

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
    """In-memory stand-in for an injected psycopg ``Connection``.

    Records every executed ``(sql, params)`` pair and every ``commit()`` so a
    test can assert what the Registry issued, and serves programmed
    ``fetchone`` / ``fetchall`` results for the read paths."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0
        self.fetchall_result: list[tuple[object, ...]] = []
        self.fetchone_result: tuple[object, ...] | None = None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    # Convenience for assertions: the verbs issued, upper-cased + whitespace-normalised.
    def statements(self) -> list[str]:
        return [" ".join(sql.split()).upper() for sql, _ in self.executed]


@pytest.fixture
def conn() -> _FakeConn:
    """A fresh fake connection per test."""
    return _FakeConn()


@pytest.fixture
def registry(conn: _FakeConn) -> Registry:
    """The real Registry over a fresh fake connection."""
    return Registry(conn)


# --- (a) FR-008 transition legality (Spec v1.3 §5.3) ---


@pytest.mark.unit
@pytest.mark.parametrize(("src", "dst"), LEGAL_EDGES)
def test_every_section_5_3_legal_edge_is_accepted(src: str, dst: str) -> None:
    """FR-008: every legal §5.3 transition passes is_legal_transition and does
    not raise via assert_legal_transition."""
    assert transitions.is_legal_transition(src, dst) is True
    transitions.assert_legal_transition(src, dst)  # must not raise


@pytest.mark.unit
def test_complete_to_running_is_rejected_without_readmission() -> None:
    """FR-008 acceptance criterion: a direct ``complete`` -> ``running`` (skipping
    re-admission via ``candidate``) is illegal and is rejected."""
    assert transitions.is_legal_transition("complete", "running") is False
    with pytest.raises(IllegalTransitionError):
        transitions.assert_legal_transition("complete", "running")


@pytest.mark.unit
def test_unknown_state_is_not_a_legal_source() -> None:
    """An out-of-enum state has no legal outgoing edge (rejected, not raised on
    the boolean check)."""
    assert transitions.is_legal_transition("nonexistent", "running") is False


# --- (b) Registry round-trip + write-boundary (Spec v1.3 §5.2-§5.5) ---


@pytest.mark.unit
def test_registry_is_a_structural_registry_port(registry: Registry) -> None:
    """The concrete Registry satisfies the OLB-01 RegistryPort seam unchanged
    (ports.py untouched)."""
    assert isinstance(registry, RegistryPort)


@pytest.mark.unit
def test_read_candidates_selects_candidate_state_and_maps_columns(
    registry: Registry, conn: _FakeConn
) -> None:
    """read_candidates issues a parameterised SELECT filtered to ``candidate`` and
    maps each row to a column->value mapping over PROJECT_COLUMNS (round-trip)."""
    row = ("p1", "Proj One", "/p1", "initiative", "active",
           "candidate", 100, None, 0, None)
    conn.fetchall_result = [row]

    result = registry.read_candidates()

    assert result == [dict(zip(PROJECT_COLUMNS, row))]
    sql, params = conn.executed[0]
    assert "WHERE lifecycle_state = %s" in sql
    assert params == ("candidate",)


@pytest.mark.unit
def test_read_running_filters_to_running_state(
    registry: Registry, conn: _FakeConn
) -> None:
    """read_running issues the same shape filtered to the ``running`` state."""
    conn.fetchall_result = []

    assert registry.read_running() == []
    _sql, params = conn.executed[0]
    assert params == ("running",)


@pytest.mark.unit
def test_read_completed_project_ids_filters_to_complete_state(
    registry: Registry, conn: _FakeConn
) -> None:
    """Item 1: read_completed_project_ids issues a parameterised SELECT filtered to the
    ``complete`` lifecycle state and returns the project_ids as a frozenset."""
    conn.fetchall_result = [("A",), ("B",)]

    result = registry.read_completed_project_ids()

    assert result == frozenset({"A", "B"})
    sql, params = conn.executed[0]
    assert "WHERE lifecycle_state = %s" in sql
    assert "project_id" in sql
    assert params == ("complete",)


@pytest.mark.unit
def test_read_completed_runs_filters_terminal_and_maps(
    registry: Registry, conn: _FakeConn
) -> None:
    """Item 2: read_completed_runs selects the terminal ralph_runs rows, renames
    project_slug -> project_id, and coerces the timestamp columns to ISO strings."""
    from datetime import datetime, timezone
    from decimal import Decimal

    spawned = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    terminated = datetime(2026, 6, 1, 0, 10, 0, tzinfo=timezone.utc)
    conn.fetchall_result = [
        ("rid", "slug", "complete", Decimal("2.50"), spawned, terminated, {"k": "v"})
    ]

    result = registry.read_completed_runs()

    assert len(result) == 1
    rec = result[0]
    assert rec["run_id"] == "rid"
    assert rec["project_id"] == "slug"
    assert rec["status"] == "complete"
    assert rec["terminal_cost_usd"] == Decimal("2.50")
    assert rec["spawned_at"] == spawned.isoformat()
    assert rec["terminated_at"] == terminated.isoformat()
    sql, _params = conn.executed[0]
    assert "status IN ('complete', 'failed')" in sql


# --- Item 2 DB capture (ol3) ---


@pytest.mark.unit
def test_upsert_learning_records_issues_upsert_per_record(
    registry: Registry, conn: _FakeConn
) -> None:
    from decimal import Decimal

    from supervisor.learn_assembly import LearningRecord

    registry.upsert_learning_records(
        [
            LearningRecord("r1", "p", "complete", Decimal("1.25"), 300.0),
            LearningRecord("r2", "p", "failed", None, None),
        ]
    )
    statements = conn.statements()
    inserts = [s for s in statements if "INSERT INTO LEARNING_RECORDS" in s]
    assert len(inserts) == 2
    assert "ON CONFLICT (RUN_ID) DO UPDATE" in inserts[0]
    assert conn.commits == 2  # one commit per record


@pytest.mark.unit
def test_upsert_audit_findings_returns_new_keys(
    registry: Registry, conn: _FakeConn
) -> None:
    from supervisor.run_auditor import AuditFinding, BindingFindingClass, FindingKind

    findings = [
        AuditFinding(
            kind=FindingKind.ANSWERER_DSL_CANDIDATE,
            subject="g1",
            evidence="e",
            recommendation="add a rule",
            routes_to="operator + cf-spec-writer",
        ),
        AuditFinding(
            kind=FindingKind.VERIFICATION_BINDING,
            subject="cf-x",
            evidence="e",
            recommendation="r",
            routes_to="operator + cf-seed-producer",
            binding_class=BindingFindingClass.OVER_VERIFICATION,
        ),
    ]
    # The pre-INSERT existence SELECT reports g1 already present → only the binding key is NEW.
    conn.fetchall_result = [("answerer_dsl_candidate:g1",)]

    new_keys = registry.upsert_audit_findings(findings, runs_audited=4)

    assert new_keys == ["verification_binding:cf-x:over_verification"]
    statements = conn.statements()
    assert any("SELECT FINDING_KEY FROM RUN_AUDIT_FINDINGS" in s for s in statements)
    assert sum("INSERT INTO RUN_AUDIT_FINDINGS" in s for s in statements) == 2


@pytest.mark.unit
def test_upsert_audit_findings_empty_is_noop(registry: Registry, conn: _FakeConn) -> None:
    assert registry.upsert_audit_findings([], runs_audited=0) == []
    assert conn.executed == []


@pytest.mark.unit
def test_read_audit_findings_maps_rows(registry: Registry, conn: _FakeConn) -> None:
    conn.fetchall_result = [
        ("k1", "session_shape", "spec_review_loop", None, "ev", "rec", "operator + x", 5)
    ]
    result = registry.read_audit_findings()
    assert result[0]["finding_key"] == "k1"
    assert result[0]["subject"] == "spec_review_loop"
    assert result[0]["runs_audited"] == 5


# --- ol4: corrections + finding lifecycle ---


@pytest.mark.unit
def test_upsert_audit_findings_writes_authoring_skill(
    registry: Registry, conn: _FakeConn
) -> None:
    from supervisor.run_auditor import AuditFinding, FindingKind

    conn.fetchall_result = []  # none existing
    registry.upsert_audit_findings(
        [
            AuditFinding(
                kind=FindingKind.ANSWERER_DSL_CANDIDATE,
                subject="g1",
                evidence="e",
                recommendation="r",
                routes_to="operator + cf-spec-writer",
            )
        ],
        runs_audited=3,
    )
    insert = next(
        (q, p) for q, p in conn.executed if "INSERT INTO run_audit_findings" in q
    )
    assert "cf-spec-writer" in insert[1]  # authoring_skill parsed from routes_to


@pytest.mark.unit
def test_set_finding_status_validates_and_writes(
    registry: Registry, conn: _FakeConn
) -> None:
    registry.set_finding_status("k1", "accepted", decided_by="greg")
    sql, params = conn.executed[0]
    assert "UPDATE run_audit_findings SET status" in sql
    assert params[0] == "accepted" and params[1] == "greg" and params[2] == "k1"
    with pytest.raises(ValueError, match="illegal finding status"):
        registry.set_finding_status("k1", "bogus", decided_by="greg")


@pytest.mark.unit
def test_upsert_correction_attempts(registry: Registry, conn: _FakeConn) -> None:
    from supervisor.learn_assembly import CorrectionAttempt

    registry.upsert_correction_attempts(
        [CorrectionAttempt("u1", "p", 2, 2, "L3", "OLB-07", "2026-06-07T10:00:00Z")]
    )
    sql, params = conn.executed[0]
    assert "INSERT INTO correction_attempts" in sql
    assert "ON CONFLICT (event_uuid) DO NOTHING" in sql
    assert params[0] == "u1" and params[5] == "OLB-07"


@pytest.mark.unit
def test_read_correction_summary_maps_rows(registry: Registry, conn: _FakeConn) -> None:
    conn.fetchall_result = [("OLB-07", 5, 2, "L4")]
    result = registry.read_correction_summary()
    assert result[0]["item_id"] == "OLB-07"
    assert result[0]["attempts"] == 5
    assert result[0]["max_level"] == "L4"


# --- ol5: fleet event persistence ---


@pytest.mark.unit
def test_upsert_events_idempotent_and_counts_new(
    registry: Registry, conn: _FakeConn
) -> None:
    conn.fetchall_result = [("u1",)]  # u1 already exists; u2 is new
    events = [
        {"event_uuid": "u1", "event_type": "gate_fire", "project_id": "p", "payload": {"a": 1}},
        {"event_uuid": "u2", "event_type": "llm_call", "project_id": "p", "payload": {"cost_usd": "1.0"}},
        {"event_type": "no_uuid"},  # skipped (no event_uuid)
    ]
    new_count = registry.upsert_events(events)
    assert new_count == 1  # only u2 was new
    inserts = [(q, p) for q, p in conn.executed if "INSERT INTO events" in q]
    assert len(inserts) == 2  # u1 + u2 both issued (u1 ON CONFLICT DO NOTHING)
    assert "ON CONFLICT (event_uuid) DO NOTHING" in inserts[0][0]
    assert "%s::jsonb" in inserts[0][0]  # payload cast to jsonb
    assert any('"a": 1' in str(p) for _q, p in inserts)  # payload serialised to JSON


@pytest.mark.unit
def test_upsert_events_empty_is_noop(registry: Registry, conn: _FakeConn) -> None:
    assert registry.upsert_events([]) == 0
    assert conn.executed == []


@pytest.mark.unit
def test_read_events_db_filters(registry: Registry, conn: _FakeConn) -> None:
    conn.fetchall_result = [
        ("u1", 1, "p", "p", 2, "gate", "gate_fire", "2026-06-07T10:00:00Z", {}, None, "g", "gate")
    ]
    result = registry.read_events_db(project_id="p", event_type="gate_fire", limit=10)
    assert result[0]["event_uuid"] == "u1" and result[0]["event_type"] == "gate_fire"
    sql, params = conn.executed[0]
    assert "WHERE project_id = %s AND event_type = %s" in sql
    assert params == ("p", "gate_fire", 10)


@pytest.mark.unit
def test_read_learning_records(registry: Registry, conn: _FakeConn) -> None:
    from decimal import Decimal

    conn.fetchall_result = [("r1", "p1", "complete", Decimal("1.25"), 300.0)]
    result = registry.read_learning_records()
    assert result[0]["project_slug"] == "p1"
    assert result[0]["cost_usd"] == Decimal("1.25")
    sql, _params = conn.executed[0]
    assert "FROM learning_records" in sql


@pytest.mark.unit
def test_read_all_projects(registry: Registry, conn: _FakeConn) -> None:
    row = ("p1", "P1", "/p1", "k", "active", "candidate", 100, None, 0, None, [])
    conn.fetchall_result = [row]
    result = registry.read_all_projects()
    assert result[0]["project_id"] == "p1"
    sql, _params = conn.executed[0]
    assert "FROM projects" in sql and "WHERE" not in sql


@pytest.mark.unit
def test_set_lifecycle_state_persists_a_legal_transition(
    registry: Registry, conn: _FakeConn
) -> None:
    """A legal ``admitted`` -> ``running`` transition issues the UPDATE and commits."""
    conn.fetchone_result = ("admitted",)  # current state read first

    registry.set_lifecycle_state("p1", "running")

    statements = conn.statements()
    assert any(s.startswith("UPDATE PROJECTS SET LIFECYCLE_STATE") for s in statements)
    assert conn.commits == 1


@pytest.mark.unit
def test_set_lifecycle_state_rejects_illegal_transition_before_any_write(
    registry: Registry, conn: _FakeConn
) -> None:
    """FR-008 at the write boundary: an illegal ``complete`` -> ``running`` raises
    and issues NO UPDATE and NO commit — the illegal transition never reaches the
    database."""
    conn.fetchone_result = ("complete",)  # current state read first

    with pytest.raises(IllegalTransitionError):
        registry.set_lifecycle_state("p1", "running")

    statements = conn.statements()
    assert not any(s.startswith("UPDATE PROJECTS") for s in statements)
    assert conn.commits == 0


@pytest.mark.unit
def test_set_lifecycle_state_same_state_is_idempotent_noop(
    registry: Registry, conn: _FakeConn
) -> None:
    """Re-asserting the state a Project is already in is a no-op (not an illegal
    self-transition): no UPDATE, no commit, no raise. This is what makes the FR-019
    admit path re-entrant — spawning a ceiling-held (already ``admitted``) Project
    re-issues ``set(admitted)`` and must not trip the ``admitted -> admitted`` guard."""
    conn.fetchone_result = ("admitted",)  # already admitted

    registry.set_lifecycle_state("p1", "admitted")  # must not raise

    statements = conn.statements()
    assert not any(s.startswith("UPDATE PROJECTS") for s in statements)
    assert conn.commits == 0


@pytest.mark.unit
def test_record_run_inserts_with_soft_project_slug_reference(
    registry: Registry, conn: _FakeConn
) -> None:
    """record_run inserts a ralph_runs row carrying ``project_slug`` (FR-010 soft
    reference) plus the supplied allowlisted columns, all parameterised, and
    commits (FR-009)."""
    registry.record_run("p1", {"run_id": "r1", "seed_path": "/seed", "status": "running"})

    sql, params = conn.executed[0]
    assert sql.startswith("INSERT INTO ralph_runs")
    assert "project_slug" in sql
    assert "p1" in params and "r1" in params and "/seed" in params
    assert conn.commits == 1


@pytest.mark.unit
def test_update_run_status_accepts_a_legal_status(
    registry: Registry, conn: _FakeConn
) -> None:
    """update_run_status writes a §5.4 CHECK-set status against the running row."""
    registry.update_run_status("p1", "complete")

    sql, params = conn.executed[0]
    assert sql.startswith("UPDATE ralph_runs SET status")
    assert params == ("complete", "p1")
    assert conn.commits == 1


@pytest.mark.unit
def test_update_run_status_rejects_out_of_set_status_before_any_write(
    registry: Registry, conn: _FakeConn
) -> None:
    """An out-of-CHECK-set status (not in RUN_STATUSES) raises and issues no
    write."""
    assert "bogus" not in RUN_STATUSES

    with pytest.raises(ValueError):
        registry.update_run_status("p1", "bogus")

    assert conn.executed == []
    assert conn.commits == 0


# --- (c) Sole-writer surface (Spec v1.3 §5.5, NFR-006) ---


@pytest.mark.unit
def test_writes_are_confined_to_the_registry_not_the_cycle_host() -> None:
    """NFR-006 sole-writer: the three RegistryPort write methods live on the
    Registry layer only; the supervision cycle host exposes none of them."""
    from supervisor.cycle import SupervisionCycle

    write_methods = {"set_lifecycle_state", "record_run", "update_run_status"}
    for name in write_methods:
        assert hasattr(Registry, name), f"Registry must own write method {name}"
        assert not hasattr(SupervisionCycle, name), (
            f"cycle host must not expose write method {name} (sole-writer breach)"
        )
