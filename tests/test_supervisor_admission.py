"""Component tests for the OLB-07 Admission Pipeline (``supervisor/admission.py``).

Covers the OLB-07 predicate (Spec v1.3 §6) DB-free, against in-memory CALL-RECORDING
fakes (a fake ``RegistryPort``, a fake ``SeedValidatorPort``, and a fake ``SpawnPort``
that can be toggled to fail) — so the FR-021 ordering and the §9.3 consumption are
asserted against the actual shipped pipeline, not a parallel re-implementation:

* (a) FR-015 discovery — ``discover_candidates`` enumerates exactly the read seam's rows.
* (b) FR-016/FR-054 seed-invalid refusal — a SEVERE ``SS-*`` finding -> ``seed_invalid``,
  finding recorded, no ``candidate -> admitted`` write; a clean seed proceeds.
* (c) FR-017 empty-registry -> ``empty_registry``.
* (d) FR-018 slug-collision -> ``slug_collision``.
* (e) FR-019 ceiling-hold — at the ceiling a clean Candidate is moved to ``admitted``
  and HELD (no ``record_run``); below the ceiling it spawns.
* (f) FR-020 blast-radius — an unresolvable scope -> ``unresolvable_blast_radius``; a
  resolvable scope is recorded on admit.
* (g) §9.3 safety-floor pass-through — a scope omitting the read-only corpus is refused
  via OLB-06 ``check_dispatch_allowed`` before spawn (consumed, not re-implemented).
* (h) FR-021 atomicity BOTH paths — success (Project ``running`` + Run row ``spawned_at``)
  and spawn-failure (Project re-gateable, NOT ``running``; Run row reconciled ``failed``).
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import pytest

from supervisor.admission import (
    EMPTY_REGISTRY,
    SEED_INVALID,
    SLUG_COLLISION,
    UNRESOLVABLE_BLAST_RADIUS,
    AdmissionRejection,
    AdmitDecision,
    AdmittedHold,
    ReconciledFailure,
    RunRecord,
    SeedFinding,
    SeedValidatorPort,
    SpawnPort,
    SpawnResult,
    admission_gate,
    admit_and_spawn,
    admit_candidate,
    discover_candidates,
)
from supervisor.ports import RegistryPort, RegistryRow
from supervisor.safety_gates import (
    READ_ONLY_CORPUS_PATH,
    READ_ONLY_INVARIANT_VIOLATION,
    BlastRadiusScope,
    KillSwitch,
)

# The global Concurrency Ceiling under test (Spec v1.3 §3 / seed §2).
CEILING = 2

# A fixed spawned_at clock so the success-path assertion is deterministic.
FIXED_SPAWNED_AT = "2026-06-05T00:00:00+00:00"

# A resolvable, FR-034-compliant Blast-Radius Scope (lists the read-only corpus) — the
# scope the FR-021 terminal-step tests spawn against. Frozen, so shared safely.
_COMPLIANT_SCOPE = BlastRadiusScope(
    read_only_paths=frozenset({READ_ONLY_CORPUS_PATH}),
    writable_paths=frozenset({"K:/work/projA"}),
)


# --- Call-recording fakes -----------------------------------------------------


class _RecordingRegistryPort:
    """Dict-backed :class:`RegistryPort` double.

    Appends the NAME of every method invoked to ``calls`` and records each
    ``set_lifecycle_state`` write as a ``(project_id, state)`` pair in
    ``lifecycle_writes`` and each ``record_run`` / ``update_run_status`` write to its own
    list — so the FR-019 (no spawn) and FR-021 (ordering / reconciliation) predicates are
    asserted against the actual writes the pipeline emits.
    """

    def __init__(self, running: Sequence[RegistryRow] = ()) -> None:
        self._running = list(running)
        self.calls: list[str] = []
        self.lifecycle_writes: list[tuple[str, str]] = []
        self.runs_recorded: list[tuple[str, RegistryRow]] = []
        self.run_status_updates: list[tuple[str, str]] = []
        self.pid_writes: list[tuple[str, int]] = []
        self.run_reconciles: list[tuple[str, str, str, Decimal]] = []

    def read_candidates(self) -> Sequence[RegistryRow]:
        self.calls.append("read_candidates")
        return []

    def read_running(self) -> Sequence[RegistryRow]:
        self.calls.append("read_running")
        return list(self._running)

    def set_lifecycle_state(self, project_id: str, state: str) -> None:
        self.calls.append("set_lifecycle_state")
        self.lifecycle_writes.append((project_id, state))

    def record_run(self, project_id: str, run: RegistryRow) -> None:
        self.calls.append("record_run")
        self.runs_recorded.append((project_id, run))

    def update_run_status(self, project_id: str, status: str) -> None:
        self.calls.append("update_run_status")
        self.run_status_updates.append((project_id, status))

    def reconcile_run(
        self,
        project_id: str,
        status: str,
        *,
        terminated_at: str,
        terminal_cost_usd: Decimal,
    ) -> None:
        self.calls.append("reconcile_run")
        self.run_reconciles.append(
            (project_id, status, terminated_at, terminal_cost_usd)
        )

    def set_run_orchestrator_pid(
        self, project_id: str, orchestrator_pid: int
    ) -> None:
        self.calls.append("set_run_orchestrator_pid")
        self.pid_writes.append((project_id, orchestrator_pid))


class _FakeSeedValidator:
    """A :class:`SeedValidatorPort` double returning a configurable ``SS-*`` finding set."""

    def __init__(self, findings: Sequence[SeedFinding] = ()) -> None:
        self._findings = list(findings)
        self.calls: list[str] = []

    def validate_seed(self, candidate: RegistryRow) -> Sequence[SeedFinding]:
        self.calls.append("validate_seed")
        return list(self._findings)


class _FakeSpawnPort:
    """A :class:`SpawnPort` double with a ``should_fail`` toggle (FR-021)."""

    def __init__(self, *, should_fail: bool = False, pid: int = 4242) -> None:
        self.should_fail = should_fail
        self.pid = pid
        self.calls: list[tuple[str, BlastRadiusScope]] = []

    def spawn(
        self, seed_path: str, blast_radius_scope: BlastRadiusScope
    ) -> SpawnResult:
        self.calls.append((seed_path, blast_radius_scope))
        if self.should_fail:
            return SpawnResult(ok=False, detail="orchestrator.sh exited non-zero")
        return SpawnResult(ok=True, orchestrator_pid=self.pid)


# --- Helpers ------------------------------------------------------------------


def _candidate(
    *,
    project_id: str = "projA",
    slug: str = "projA",
    open_items: int = 3,
    read_only: Sequence[str] = (READ_ONLY_CORPUS_PATH,),
    writable: Sequence[str] = ("K:/work/projA",),
    mcp_roots: Sequence[str] = (),
) -> dict[str, object]:
    """A Candidate row whose defaults clear every precondition with a compliant scope."""
    return {
        "project_id": project_id,
        "initiative_slug": slug,
        "seed_path": f"K:/seeds/{project_id}/seed.json",
        "open_item_count": open_items,
        "read_only_paths": list(read_only),
        "writable_paths": list(writable),
        "mcp_roots": list(mcp_roots),
    }


def _fixed_clock() -> str:
    return FIXED_SPAWNED_AT


@pytest.fixture
def seed_ok() -> _FakeSeedValidator:
    return _FakeSeedValidator()


@pytest.fixture
def spawn_ok() -> _FakeSpawnPort:
    return _FakeSpawnPort()


# --- structural conformance: the fakes satisfy the shipped Protocols ----------


@pytest.mark.unit
def test_fakes_are_structural_ports(
    seed_ok: _FakeSeedValidator, spawn_ok: _FakeSpawnPort
) -> None:
    """The fakes satisfy the OLB-02 + OLB-07 seams (ports.py untouched; the new Ports are
    consumed structurally)."""
    assert isinstance(_RecordingRegistryPort(), RegistryPort)
    assert isinstance(seed_ok, SeedValidatorPort)
    assert isinstance(spawn_ok, SpawnPort)


# --- (a) FR-015 candidate discovery -------------------------------------------


@pytest.mark.unit
def test_fr015_discover_candidates_enumerates_read_seam() -> None:
    """FR-015: ``discover_candidates`` returns exactly the read seam's rows — no
    filesystem re-scan; the OLB-02 ``read_candidates`` read method is consulted."""

    class _TwoCandidates(_RecordingRegistryPort):
        def read_candidates(self) -> Sequence[RegistryRow]:
            self.calls.append("read_candidates")
            return [_candidate(project_id="a"), _candidate(project_id="b")]

    port = _TwoCandidates()
    found = discover_candidates(port)

    assert [row["project_id"] for row in found] == ["a", "b"]
    assert port.calls == ["read_candidates"]


# --- (b) FR-016 / FR-054 seed-validity refusal --------------------------------


@pytest.mark.unit
def test_fr016_severe_finding_refuses_and_blocks_admitted() -> None:
    """FR-016/FR-054: a SEVERE ``SS-*`` finding -> reason ``seed_invalid``, the finding is
    recorded, and NO ``candidate -> admitted`` transition occurs (the hook blocks it)."""
    finding = SeedFinding(code="SS-007", severity="SEVERE", detail="seed missing design zone")
    port = _RecordingRegistryPort()
    result = admit_candidate(
        _candidate(),
        seed_validator=_FakeSeedValidator([finding]),
        registry_port=port,
        spawn_port=_FakeSpawnPort(),
        kill_switch=KillSwitch(),
        running_count=0,
        concurrency_ceiling=CEILING,
    )

    assert isinstance(result, AdmissionRejection)
    assert result.reason == SEED_INVALID
    assert result.seed_finding is finding
    # The seed-validation hook blocked admission: no lifecycle write at all.
    assert "set_lifecycle_state" not in port.calls
    assert ("projA", "admitted") not in port.lifecycle_writes


@pytest.mark.unit
def test_fr016_sub_severe_finding_proceeds_past_hook() -> None:
    """A finding below SEVERE does not block the hook — admission proceeds and spawns."""
    finding = SeedFinding(code="SS-019", severity="ADVISORY", detail="cosmetic")
    port = _RecordingRegistryPort()
    result = admit_candidate(
        _candidate(),
        seed_validator=_FakeSeedValidator([finding]),
        registry_port=port,
        spawn_port=_FakeSpawnPort(),
        kill_switch=KillSwitch(),
        running_count=0,
        concurrency_ceiling=CEILING,
    )

    assert isinstance(result, RunRecord)


# --- (c) FR-017 empty-registry ------------------------------------------------


@pytest.mark.unit
def test_fr017_empty_registry_refused(seed_ok: _FakeSeedValidator) -> None:
    """FR-017: a Candidate whose work registry holds no open item -> ``empty_registry``."""
    result = admission_gate(
        _candidate(open_items=0),
        seed_validator=seed_ok,
        registry_port=_RecordingRegistryPort(),
        kill_switch=KillSwitch(),
        running_count=0,
        concurrency_ceiling=CEILING,
    )

    assert isinstance(result, AdmissionRejection)
    assert result.reason == EMPTY_REGISTRY


# --- (d) FR-018 slug-collision ------------------------------------------------


@pytest.mark.unit
def test_fr018_slug_collision_refused(seed_ok: _FakeSeedValidator) -> None:
    """FR-018: ``initiative.slug`` colliding with a running ``project_id`` ->
    ``slug_collision``."""
    port = _RecordingRegistryPort(running=[{"project_id": "projA"}])
    result = admission_gate(
        _candidate(slug="projA"),
        seed_validator=seed_ok,
        registry_port=port,
        kill_switch=KillSwitch(),
        running_count=1,
        concurrency_ceiling=CEILING,
    )

    assert isinstance(result, AdmissionRejection)
    assert result.reason == SLUG_COLLISION


# --- (e) FR-019 ceiling-hold --------------------------------------------------


@pytest.mark.unit
def test_fr019_at_ceiling_holds_in_admitted_no_spawn(
    seed_ok: _FakeSeedValidator,
) -> None:
    """FR-019: at the ceiling a Candidate clearing every other precondition is moved to
    ``admitted`` and HELD — no Run is spawned (no ``record_run``)."""
    port = _RecordingRegistryPort()
    result = admit_candidate(
        _candidate(),
        seed_validator=seed_ok,
        registry_port=port,
        spawn_port=_FakeSpawnPort(),
        kill_switch=KillSwitch(),
        running_count=CEILING,  # at the ceiling: running_count + 1 > ceiling
        concurrency_ceiling=CEILING,
    )

    assert isinstance(result, AdmittedHold)
    # Held in `admitted` only — no spawn machinery ran.
    assert ("projA", "admitted") in port.lifecycle_writes
    assert "record_run" not in port.calls
    assert ("projA", "running") not in port.lifecycle_writes


@pytest.mark.unit
def test_fr019_below_ceiling_proceeds_to_admit(seed_ok: _FakeSeedValidator) -> None:
    """Below the ceiling the same Candidate clears the gate to an AdmitDecision."""
    decision = admission_gate(
        _candidate(),
        seed_validator=seed_ok,
        registry_port=_RecordingRegistryPort(),
        kill_switch=KillSwitch(),
        running_count=CEILING - 1,
        concurrency_ceiling=CEILING,
    )

    assert isinstance(decision, AdmitDecision)


# --- (f) FR-020 blast-radius declaration --------------------------------------


@pytest.mark.unit
def test_fr020_unresolvable_blast_radius_refused(seed_ok: _FakeSeedValidator) -> None:
    """FR-020: a seed with no derivable substrate (no writable path, no MCP root) ->
    ``unresolvable_blast_radius``."""
    result = admission_gate(
        _candidate(writable=(), mcp_roots=()),
        seed_validator=seed_ok,
        registry_port=_RecordingRegistryPort(),
        kill_switch=KillSwitch(),
        running_count=0,
        concurrency_ceiling=CEILING,
    )

    assert isinstance(result, AdmissionRejection)
    assert result.reason == UNRESOLVABLE_BLAST_RADIUS


@pytest.mark.unit
def test_fr020_resolvable_scope_recorded_on_admit(seed_ok: _FakeSeedValidator) -> None:
    """FR-020: a resolvable scope is recorded on the admit decision (via OLB-06
    ``provision_blast_radius`` — exactly the seed's declared boundaries)."""
    decision = admission_gate(
        _candidate(writable=("K:/work/projA",), mcp_roots=("mcp://projA",)),
        seed_validator=seed_ok,
        registry_port=_RecordingRegistryPort(),
        kill_switch=KillSwitch(),
        running_count=0,
        concurrency_ceiling=CEILING,
    )

    assert isinstance(decision, AdmitDecision)
    assert decision.blast_radius_scope.writable_paths == frozenset({"K:/work/projA"})
    assert decision.blast_radius_scope.mcp_roots == frozenset({"mcp://projA"})


# --- (g) §9.3 safety-floor pass-through ---------------------------------------


@pytest.mark.unit
def test_section_9_3_scope_omitting_read_only_corpus_refused_before_spawn(
    seed_ok: _FakeSeedValidator,
) -> None:
    """§9.3: a (resolvable) scope omitting the read-only corpus is refused via OLB-06
    ``check_dispatch_allowed`` BEFORE any spawn — the safety reason is passed through and
    no Run is recorded."""
    port = _RecordingRegistryPort()
    spawn = _FakeSpawnPort()
    result = admit_candidate(
        _candidate(read_only=("K:/work/projA",)),  # resolvable, but omits the corpus
        seed_validator=seed_ok,
        registry_port=port,
        spawn_port=spawn,
        kill_switch=KillSwitch(),
        running_count=0,
        concurrency_ceiling=CEILING,
    )

    assert isinstance(result, AdmissionRejection)
    assert result.reason == READ_ONLY_INVARIANT_VIOLATION
    assert result.safety_refusal is not None
    assert spawn.calls == []  # refused before spawn
    assert "record_run" not in port.calls


# --- (h) FR-021 admit-and-spawn atomicity — BOTH paths ------------------------


@pytest.mark.unit
def test_fr021_success_project_running_run_row_spawned_at() -> None:
    """FR-021 success: the Project reads ``running`` and a Run row carries ``spawned_at``
    (+ the ``orchestrator_pid`` boundary); the Run row is written BEFORE the running
    transition."""
    port = _RecordingRegistryPort()
    spawn = _FakeSpawnPort(should_fail=False)
    result = admit_and_spawn(
        _candidate(),
        registry_port=port,
        spawn_port=spawn,
        blast_radius_scope=_COMPLIANT_SCOPE,
        clock=_fixed_clock,
    )

    assert isinstance(result, RunRecord)
    assert result.lifecycle_state == "running"
    assert result.spawned_at == FIXED_SPAWNED_AT
    assert result.orchestrator_pid == spawn.pid
    # Project ends `running`; a Run row exists with spawned_at.
    assert ("projA", "running") in port.lifecycle_writes
    assert port.runs_recorded[0][1]["spawned_at"] == FIXED_SPAWNED_AT
    # Ordering: the Run row is written BEFORE the running transition (so a failure has a
    # reconcilable row) — no terminal reconciliation on the success path.
    assert port.calls.index("record_run") < port.calls.index("set_lifecycle_state", 1)
    assert "update_run_status" not in port.calls


@pytest.mark.unit
def test_fr021_record_run_forwards_seed_path() -> None:
    """FR-021 spawn-row completeness: the pre-spawn Run row carries the candidate's
    ``seed_path`` (Spec v1.3 §5.4 — the ``seed_path`` column is NOT NULL in the production
    ``ralph_runs`` shape). Regression for the iter-0017 first-live-C2 NotNullViolation:
    the Run mapping omitted ``seed_path`` so the concrete INSERT violated the constraint."""
    port = _RecordingRegistryPort()
    candidate = _candidate()
    result = admit_and_spawn(
        candidate,
        registry_port=port,
        spawn_port=_FakeSpawnPort(should_fail=False),
        blast_radius_scope=_COMPLIANT_SCOPE,
        clock=_fixed_clock,
    )

    assert isinstance(result, RunRecord)
    # The pre-spawn Run row forwards the candidate's seed_path alongside status/spawned_at.
    recorded_run = port.runs_recorded[0][1]
    assert recorded_run["seed_path"] == candidate["seed_path"]
    assert recorded_run["status"] == "running"
    assert recorded_run["spawned_at"] == FIXED_SPAWNED_AT


@pytest.mark.unit
def test_fr021_spawn_failure_regateable_and_run_reconciled_failed() -> None:
    """FR-021 spawn-failure: the Project is left re-gateable (NOT ``running``) and the Run
    row is reconciled to ``failed`` — so no ``running`` Project lacks a reconcilable Run
    row."""
    port = _RecordingRegistryPort()
    spawn = _FakeSpawnPort(should_fail=True)
    result = admit_and_spawn(
        _candidate(),
        registry_port=port,
        spawn_port=spawn,
        blast_radius_scope=_COMPLIANT_SCOPE,
        clock=_fixed_clock,
    )

    assert isinstance(result, ReconciledFailure)
    assert result.re_gateable is True
    assert result.run_status == "failed"
    # The Run row was reconciled to `failed` ...
    assert ("projA", "failed") in port.run_status_updates
    # ... and the Project is left re-gateable (`failed`), NOT stuck `running`.
    assert ("projA", "failed") in port.lifecycle_writes
    assert port.lifecycle_writes[-1] == ("projA", "failed")
    # A Run row existed BEFORE the failure (record_run preceded update_run_status).
    assert port.calls.index("record_run") < port.calls.index("update_run_status")


@pytest.mark.unit
def test_fr021_spawn_raises_is_reconciled_not_propagated() -> None:
    """A spawn that RAISES is reconciled like an ``ok=False`` return — the exception does
    not escape and no ``running`` Project is left without a reconciled Run row."""

    class _RaisingSpawn:
        def spawn(self, seed_path: str, blast_radius_scope: BlastRadiusScope) -> SpawnResult:
            raise RuntimeError("orchestrator.sh not found")

    port = _RecordingRegistryPort()
    result = admit_and_spawn(
        _candidate(),
        registry_port=port,
        spawn_port=_RaisingSpawn(),
        blast_radius_scope=_COMPLIANT_SCOPE,
        clock=_fixed_clock,
    )

    assert isinstance(result, ReconciledFailure)
    assert ("projA", "failed") in port.run_status_updates
    assert ("projA", "failed") in port.lifecycle_writes
