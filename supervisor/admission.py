"""Admission Pipeline for the Outer Loop Supervisor (OLB-07).

A substrate-light decision/orchestration layer encoding the Spec v1.3 §6 Admission
Gate — the conjunction of preconditions that is the *only* path by which a Candidate
becomes a ``running`` Project (§6.1). Admission never partially admits: it ends in
exactly one of

* **admitted-and-spawned** — every precondition cleared, headroom available, the
  orchestrator spawned (FR-021);
* **admitted-held** — every precondition cleared but the Concurrency Ceiling would be
  exceeded, so the Project is moved to ``admitted`` and HELD, spawning nothing until
  headroom appears (FR-019); or
* **rejected-with-reason** — the first failing precondition's structured reason, or a
  §9.3 safety-gate refusal passed through.

Resolved per gate ``olb07-admission-build-scope`` (option A — the proposed default):
OLB-07 ships the pure DB-free decision/orchestration layer. The seed validity check
(FR-016/FR-054) and the orchestrator spawn (FR-021/§6.3) sit behind injectable Ports;
the LIVE wiring behind them is forward-referenced to OLB-08 (C2 single-project
end-to-end):

* FR-016/FR-054 seed-validity — :class:`SeedValidatorPort` returns the cf-seed-reviewer
  ``SS-*`` finding set; a finding at :data:`SEVERITY_SEVERE` blocks ``candidate ->
  admitted`` with reason :data:`SEED_INVALID`. The live cf-seed-reviewer invocation
  behind the port is OLB-08.
* FR-021 admit-and-spawn — :class:`SpawnPort` invokes ``orchestrator.sh`` (§6.3); the
  live invocation behind the port is OLB-08.

This module is a CONSUMER of two seams it never edits or re-implements:

* the OLB-02 :class:`~supervisor.ports.RegistryPort` (reads for discovery/collision,
  the ``set_lifecycle_state`` / ``record_run`` / ``update_run_status`` writes for the
  FR-021 terminal step); and
* the OLB-06 Safety-Gates floor — :func:`~supervisor.safety_gates.check_dispatch_allowed`
  is consulted before every spawn so the §9.3 safety precedence (FR-034 read-only
  corpus, FR-036 Kill-Switch, FR-037 ceiling hard bound) gates the admission decision,
  and :func:`~supervisor.safety_gates.provision_blast_radius` records the FR-020
  Blast-Radius Scope. The ceiling/read-only logic is consulted, never duplicated here.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol, runtime_checkable

from supervisor.ports import RegistryPort, RegistryRow
from supervisor.safety_gates import (
    CONCURRENCY_CEILING_EXCEEDED,
    DEFAULT_CONCURRENCY_CEILING,
    REFUSAL_REASONS,
    BlastRadiusScope,
    DispatchRefusal,
    KillSwitch,
    check_dispatch_allowed,
    provision_blast_radius,
)

# --- Canonical rejection reason codes (§6.1) ---------------------------------
# One canonical rejection shape carries one of these structured codes; no ad-hoc
# rejection strings are produced anywhere in this module.

#: FR-016/FR-054 — the seed is invalid (a SEVERE ``SS-*`` finding was recorded).
SEED_INVALID = "seed_invalid"
#: FR-017 — the Candidate's work registry holds no ``open`` item.
EMPTY_REGISTRY = "empty_registry"
#: FR-018 — ``initiative.slug`` collides with an admitted-or-running ``project_id``.
SLUG_COLLISION = "slug_collision"
#: FR-020 — no Blast-Radius Scope is derivable from the Candidate's seed.
UNRESOLVABLE_BLAST_RADIUS = "unresolvable_blast_radius"

#: The admission-side reasons. A §9.3 safety-gate refusal additionally passes its own
#: canonical reason through (:data:`~supervisor.safety_gates.REFUSAL_REASONS`).
ADMISSION_REASONS = frozenset(
    {SEED_INVALID, EMPTY_REGISTRY, SLUG_COLLISION, UNRESOLVABLE_BLAST_RADIUS}
)
#: The closed set of reasons an :class:`AdmissionRejection` may carry — the admission
#: reasons plus the pass-through safety refusals (§9.3 precedence).
REJECTION_REASONS = ADMISSION_REASONS | REFUSAL_REASONS

# The severity at which a cf-seed-reviewer ``SS-*`` finding blocks admission (FR-016 —
# the SS-* rule set is consulted at SEVERE per Subproject_Folder_Convention §8).
SEVERITY_SEVERE = "SEVERE"

# The lifecycle states the FR-021 terminal step writes through the RegistryPort seam.
_STATE_ADMITTED = "admitted"
_STATE_RUNNING = "running"
_STATE_FAILED = "failed"

# The Run status the FR-021 spawn-failure path reconciles a Run row to.
_RUN_STATUS_RUNNING = "running"
_RUN_STATUS_FAILED = "failed"


# --- Injectable seed-validator seam (FR-016 / FR-054) ------------------------


@dataclass(frozen=True)
class SeedFinding:
    """A single cf-seed-reviewer finding (Spec v1.3 §6.2 FR-016).

    ``code`` is the ``SS-*`` rule id; ``severity`` is its severity label (a finding at
    :data:`SEVERITY_SEVERE` blocks admission). ``detail`` is the human-readable note
    recorded as the rejection reason when the finding refuses admission.
    """

    code: str
    severity: str
    detail: str = ""

    def is_severe(self) -> bool:
        """True iff this finding is at :data:`SEVERITY_SEVERE` (case-insensitive)."""
        return self.severity.upper() == SEVERITY_SEVERE


@runtime_checkable
class SeedValidatorPort(Protocol):
    """The seed-validation admission hook seam (Spec v1.3 §14 FR-054, §6.2 FR-016).

    Returns the cf-seed-reviewer ``SS-*`` finding set for a Candidate's seed. The live
    wiring that runs the SS-* rule set against a real seed at SEVERE is OLB-08 (C2); the
    unit suite drives this with a fake.
    """

    def validate_seed(self, candidate: RegistryRow) -> Sequence[SeedFinding]:
        """Return the ``SS-*`` findings for ``candidate``'s seed; empty when clean."""
        ...


# --- Injectable spawn seam (FR-021 / §6.3) -----------------------------------


@dataclass(frozen=True)
class SpawnResult:
    """The result of a :meth:`SpawnPort.spawn` invocation (Spec v1.3 §6.3).

    ``ok`` is the success flag; ``orchestrator_pid`` is the spawned Run's pid (the §6.3
    active boundary, paired with ``spawned_at``) on success; ``detail`` carries the
    failure note on the spawn-failure path.
    """

    ok: bool
    orchestrator_pid: int | None = None
    detail: str = ""


@runtime_checkable
class SpawnPort(Protocol):
    """The orchestrator-spawn seam (Spec v1.3 §6.3 FR-021).

    ``spawn`` invokes ``orchestrator.sh`` against the Candidate's seed provisioned with
    exactly the recorded Blast-Radius Scope and no broader. The live ``orchestrator.sh``
    invocation behind this port is OLB-08 (C2); the unit suite drives it with a fake that
    can be made to fail.
    """

    def spawn(
        self, seed_path: str, blast_radius_scope: BlastRadiusScope
    ) -> SpawnResult:
        """Spawn the orchestrator Run for the seed at ``seed_path`` confined to
        ``blast_radius_scope``. May raise or return ``ok=False`` to signal failure."""
        ...


# --- Admission decision result types (§6.1) ----------------------------------


@dataclass(frozen=True)
class AdmissionRejection:
    """The single canonical rejection shape (Spec v1.3 §6.1).

    Carries a structured ``reason`` (one of :data:`REJECTION_REASONS`) and a
    human-readable ``detail``. ``seed_finding`` carries the SEVERE ``SS-*`` finding when
    the reason is :data:`SEED_INVALID`; ``safety_refusal`` carries the OLB-06 refusal
    when a §9.3 safety-gate refusal was passed through. Falsey, so callers may write
    ``if not result:``.
    """

    reason: str
    detail: str
    seed_finding: SeedFinding | None = None
    safety_refusal: DispatchRefusal | None = None

    def __post_init__(self) -> None:
        if self.reason not in REJECTION_REASONS:
            raise ValueError(
                f"unknown rejection reason {self.reason!r}; "
                f"expected one of {sorted(REJECTION_REASONS)}"
            )

    def __bool__(self) -> bool:
        return False


@dataclass(frozen=True)
class AdmitDecision:
    """The gate passed AND headroom is available — proceed to :func:`admit_and_spawn`.

    Carries the ``project_id``, the ``seed_path`` to spawn, and the recorded FR-020
    ``blast_radius_scope`` (already run through OLB-06 :func:`provision_blast_radius`).
    Truthy.
    """

    project_id: str
    seed_path: str
    blast_radius_scope: BlastRadiusScope

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True)
class AdmittedHold:
    """FR-019 ceiling hold — the gate passed but the Concurrency Ceiling would be
    exceeded, so the Project is moved to ``admitted`` and HELD; nothing is spawned until
    headroom appears (Spec v1.3 §6.2 FR-019). Carries the recorded Blast-Radius Scope so
    the later spawn uses exactly it. Truthy.
    """

    project_id: str
    blast_radius_scope: BlastRadiusScope

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True)
class RunRecord:
    """The FR-021 admit-and-spawn SUCCESS result (Spec v1.3 §6.2 FR-021).

    The Project reads ``running`` and a Run row exists carrying ``spawned_at`` (+ the
    ``orchestrator_pid`` §6.3 active boundary). Truthy.
    """

    project_id: str
    spawned_at: str
    orchestrator_pid: int | None = None
    lifecycle_state: str = _STATE_RUNNING

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True)
class ReconciledFailure:
    """The FR-021 admit-and-spawn SPAWN-FAILURE result (Spec v1.3 §6.2 FR-021).

    The orchestrator spawn failed; the ordering guarantees no ``running`` Project is left
    without a reconcilable Run row, so the Run row is reconciled to ``failed`` and the
    Project is left re-gateable (``failed``, NOT stuck ``running`` — ``failed ->
    candidate`` re-admission is legal). Falsey.
    """

    project_id: str
    run_status: str = _RUN_STATUS_FAILED
    lifecycle_state: str = _STATE_FAILED
    re_gateable: bool = True
    detail: str = ""

    def __bool__(self) -> bool:
        return False


# --- FR-015 candidate discovery ----------------------------------------------


def discover_candidates(port: RegistryPort) -> Sequence[RegistryRow]:
    """Enumerate Candidate Projects via the OLB-02 read seam (Spec v1.3 §6.2 FR-015).

    Returns ``port.read_candidates()`` — the read seam already encodes "seed + non-empty
    work registry, no admitted-or-later lifecycle state"; this performs NO filesystem
    re-scan.
    """
    return list(port.read_candidates())


# --- The Admission Gate: the FR-016–020 precondition conjunction (§6.1) -------


def admission_gate(
    candidate: RegistryRow,
    *,
    seed_validator: SeedValidatorPort,
    registry_port: RegistryPort,
    kill_switch: KillSwitch,
    running_count: int,
    concurrency_ceiling: int = DEFAULT_CONCURRENCY_CEILING,
) -> AdmitDecision | AdmittedHold | AdmissionRejection:
    """Evaluate the §6 Admission Gate for one Candidate — a PURE decision (no write).

    The preconditions are evaluated in a fixed order, short-circuiting to the first
    failing precondition's recorded reason:

    1. **FR-016 / FR-054 seed-validity** — a SEVERE ``SS-*`` finding blocks ``candidate
       -> admitted`` with reason :data:`SEED_INVALID` (the finding recorded);
    2. **FR-017 non-empty-registry** — no ``open`` item -> :data:`EMPTY_REGISTRY`;
    3. **FR-018 slug-collision** — ``initiative.slug`` equals a running ``project_id``
       -> :data:`SLUG_COLLISION`;
    4. **FR-020 blast-radius declaration** — no scope derivable from the seed ->
       :data:`UNRESOLVABLE_BLAST_RADIUS`; otherwise the scope is recorded via OLB-06
       :func:`provision_blast_radius`;
    5. **§9.3 safety floor** — OLB-06 :func:`check_dispatch_allowed` is consulted (it
       owns the FR-034 read-only-corpus, FR-036 Kill-Switch, and FR-037 ceiling hard
       bound). A ceiling-exceeded refusal is interpreted by admission as the **FR-019
       hold** (an :class:`AdmittedHold`, not a rejection); any other safety refusal is
       passed through as an :class:`AdmissionRejection` (§9.3 — safety wins over admit).

    Reads only (``read_running`` for collision); writes nothing. Returns an
    :class:`AdmitDecision` when every precondition clears with headroom, an
    :class:`AdmittedHold` at the ceiling, or an :class:`AdmissionRejection` otherwise.
    """
    project_id = _project_id(candidate)

    # 1. FR-016 / FR-054 — the seed-validation admission hook blocks candidate->admitted.
    severe = _first_severe_finding(seed_validator.validate_seed(candidate))
    if severe is not None:
        return AdmissionRejection(
            reason=SEED_INVALID,
            detail=(
                f"seed-validity FR-016 refused: SEVERE finding {severe.code} — "
                f"{severe.detail}"
            ),
            seed_finding=severe,
        )

    # 2. FR-017 — the work registry must hold at least one open item.
    if _open_item_count(candidate) < 1:
        return AdmissionRejection(
            reason=EMPTY_REGISTRY,
            detail=f"work registry holds no open item for {project_id!r} (FR-017).",
        )

    # 3. FR-018 — the initiative slug must not collide with a running project_id.
    slug = _slug(candidate)
    running_ids = {_project_id(row) for row in registry_port.read_running()}
    if slug in running_ids:
        return AdmissionRejection(
            reason=SLUG_COLLISION,
            detail=f"initiative slug {slug!r} collides with a running project (FR-018).",
        )

    # 4. FR-020 — a Blast-Radius Scope must be derivable from the seed; record it via
    #    the OLB-06 primitive (exactly the recorded scope, no broader).
    resolved = _resolve_blast_radius(candidate)
    if resolved is None:
        return AdmissionRejection(
            reason=UNRESOLVABLE_BLAST_RADIUS,
            detail=(
                f"no Blast-Radius Scope derivable from seed for {project_id!r} "
                f"(FR-020)."
            ),
        )
    scope = provision_blast_radius(resolved)

    # 5. §9.3 safety floor — consult OLB-06 (it owns FR-034/FR-036/FR-037); do not
    #    re-implement the read-only-corpus or ceiling logic here.
    decision = check_dispatch_allowed(
        blast_radius_scope=scope,
        running_count=running_count,
        kill_switch=kill_switch,
        concurrency_ceiling=concurrency_ceiling,
        requested_by="admission",
    )
    if not decision:
        refusal = decision.refusal
        if refusal is None:
            # A refused DispatchDecision always carries a refusal (OLB-06 contract); a
            # refusal-less refused decision is a seam violation, surfaced — never a
            # silent admit. (Explicit guard, not an ``assert``: it must survive ``-O``.)
            raise ValueError(
                "OLB-06 check_dispatch_allowed returned a refused decision "
                "without a refusal"
            )
        if refusal.reason == CONCURRENCY_CEILING_EXCEEDED:
            # FR-019 — admission interprets the ceiling hard bound as a HOLD: move to
            # `admitted` and spawn nothing until headroom (not a rejection).
            return AdmittedHold(project_id=project_id, blast_radius_scope=scope)
        # FR-034 / FR-036 — §9.3 precedence: the safety refusal wins over admit and is
        # passed through as the canonical rejection.
        return AdmissionRejection(
            reason=refusal.reason, detail=refusal.detail, safety_refusal=refusal
        )

    # Every precondition cleared and headroom exists -> admit (the FR-021 terminal step
    # spawns).
    return AdmitDecision(
        project_id=project_id,
        seed_path=_seed_path(candidate),
        blast_radius_scope=scope,
    )


# --- FR-021 admit-and-spawn atomicity (the single terminal step, §6.3) -------


def _utc_now_iso() -> str:
    """The default ``spawned_at`` clock — an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def admit_and_spawn(
    candidate: RegistryRow,
    *,
    registry_port: RegistryPort,
    spawn_port: SpawnPort,
    blast_radius_scope: BlastRadiusScope,
    clock: Callable[[], str] = _utc_now_iso,
) -> RunRecord | ReconciledFailure:
    """Transition an admitted Candidate to ``running`` and spawn its orchestrator — the
    single FR-021 terminal step (Spec v1.3 §6.2 FR-021, §6.3).

    The write order is the correctness core: the Run row is written BEFORE the
    ``admitted -> running`` transition and BEFORE the spawn, so a spawn failure can NEVER
    leave a ``running`` Project without a reconcilable Run row. On success the Project
    reads ``running`` and the Run row carries ``spawned_at`` (+ the ``orchestrator_pid``
    §6.3 boundary). On spawn failure the Run row is reconciled to ``failed`` and the
    Project is moved off ``running`` to ``failed`` (re-gateable via ``failed ->
    candidate``); the Supervisor never edits the seed and never writes the spawned
    Project's ``state_dir``.

    ``spawn_port.spawn`` failure is taken from EITHER a raised exception OR a returned
    ``SpawnResult(ok=False)``.
    """
    project_id = _project_id(candidate)
    seed_path = _seed_path(candidate)
    spawned_at = clock()

    # candidate -> admitted (the gate passed; this edge is legal per §5.3).
    registry_port.set_lifecycle_state(project_id, _STATE_ADMITTED)
    # Run row written BEFORE the running transition + spawn: any spawn failure from here
    # on leaves a reconcilable Run row (FR-021 / §6.3 ordering).
    registry_port.record_run(
        project_id, {"status": _RUN_STATUS_RUNNING, "spawned_at": spawned_at}
    )
    # admitted -> running.
    registry_port.set_lifecycle_state(project_id, _STATE_RUNNING)

    # Spawn the orchestrator (§6.3) — the only spawn site (live orchestrator.sh wiring
    # behind SpawnPort deferred to OLB-08).
    try:
        result = spawn_port.spawn(seed_path, blast_radius_scope)
    except Exception as exc:  # noqa: BLE001 - ANY spawn failure must reconcile, not raise
        result = SpawnResult(ok=False, detail=f"spawn raised: {exc!r}")

    if not result.ok:
        # Reconcile: Run row -> failed, Project off `running` -> failed (re-gateable).
        registry_port.update_run_status(project_id, _RUN_STATUS_FAILED)
        registry_port.set_lifecycle_state(project_id, _STATE_FAILED)
        return ReconciledFailure(project_id=project_id, detail=result.detail)

    # FR-009 — persist the spawned orchestrator pid on the Run row now that the
    # spawn cleared (the §6.3 active boundary, paired with spawned_at). Strictly
    # additive after the spawn; the FR-021 pre-spawn record_run ordering above is
    # untouched. Skipped when the SpawnPort returns no pid (None) so a pid-less
    # success never issues a null-pid write.
    if result.orchestrator_pid is not None:
        registry_port.set_run_orchestrator_pid(project_id, result.orchestrator_pid)

    return RunRecord(
        project_id=project_id,
        spawned_at=spawned_at,
        orchestrator_pid=result.orchestrator_pid,
    )


# --- The full pipeline: the only path Candidate -> running (§6.1) -------------


def admit_candidate(
    candidate: RegistryRow,
    *,
    seed_validator: SeedValidatorPort,
    registry_port: RegistryPort,
    spawn_port: SpawnPort,
    kill_switch: KillSwitch,
    running_count: int,
    concurrency_ceiling: int = DEFAULT_CONCURRENCY_CEILING,
    clock: Callable[[], str] = _utc_now_iso,
) -> RunRecord | ReconciledFailure | AdmittedHold | AdmissionRejection:
    """Run one Candidate through the §6 Admission Pipeline — the only path Candidate ->
    ``running`` (Spec v1.3 §6.1). Admission ends in exactly one outcome:

    * :class:`RunRecord` — admitted-and-spawned (FR-021 success);
    * :class:`ReconciledFailure` — admitted but the spawn failed, reconciled (FR-021);
    * :class:`AdmittedHold` — admitted and HELD at the ceiling, nothing spawned (FR-019);
    * :class:`AdmissionRejection` — rejected with a recorded reason (FR-016–020 / §9.3).

    Never partially admits: the gate decision wholly determines which single terminal
    action runs.
    """
    decision = admission_gate(
        candidate,
        seed_validator=seed_validator,
        registry_port=registry_port,
        kill_switch=kill_switch,
        running_count=running_count,
        concurrency_ceiling=concurrency_ceiling,
    )
    if isinstance(decision, AdmissionRejection):
        return decision
    if isinstance(decision, AdmittedHold):
        # FR-019 — transition candidate -> admitted and HOLD: no Run row, no spawn.
        registry_port.set_lifecycle_state(decision.project_id, _STATE_ADMITTED)
        return decision
    # AdmitDecision — headroom + safety floor cleared: the FR-021 terminal step.
    return admit_and_spawn(
        candidate,
        registry_port=registry_port,
        spawn_port=spawn_port,
        blast_radius_scope=decision.blast_radius_scope,
        clock=clock,
    )


# --- Candidate-row accessors (the seed shape FR-015 surfaces) -----------------


def _project_id(row: RegistryRow) -> str:
    """The Project id of a Registry row (Spec v1.3 §5.2)."""
    return str(row["project_id"])


def _slug(candidate: RegistryRow) -> str:
    """The Candidate's ``initiative.slug`` (FR-018), falling back to ``project_id``."""
    for key in ("initiative_slug", "slug"):
        value = candidate.get(key)
        if value is not None:
            return str(value)
    return _project_id(candidate)


def _seed_path(candidate: RegistryRow) -> str:
    """The Candidate's seed path — the §6.3 spawn target."""
    return str(candidate.get("seed_path", ""))


def _open_item_count(candidate: RegistryRow) -> int:
    """The number of ``open`` work-registry items (FR-017).

    Reads ``open_item_count`` directly, or counts an ``open_items`` collection.
    """
    count = candidate.get("open_item_count")
    if isinstance(count, (int, float)) and not isinstance(count, bool):
        return int(count)
    items = candidate.get("open_items")
    if isinstance(items, Sequence) and not isinstance(items, str):
        return len(items)
    return 0


def _resolve_blast_radius(candidate: RegistryRow) -> BlastRadiusScope | None:
    """Derive a Blast-Radius Scope from the Candidate's seed (FR-020).

    The scope is built from the seed's ``read_only_paths`` + ``writable_paths`` +
    ``mcp_roots`` (+ optional ``design_zone``). It is UNRESOLVABLE — returns ``None`` —
    when the seed declares no owned substrate at all (neither a writable path nor an MCP
    root), because there is then no boundary to confine a Run to. (A scope that omits the
    read-only corpus is still resolvable here; that omission is caught downstream by the
    §9.3 FR-034 safety floor, not by FR-020.)
    """
    writable = _str_set(candidate.get("writable_paths"))
    mcp_roots = _str_set(candidate.get("mcp_roots"))
    if not writable and not mcp_roots:
        return None
    design_zone = candidate.get("design_zone")
    return BlastRadiusScope(
        read_only_paths=_str_set(candidate.get("read_only_paths")),
        writable_paths=writable,
        mcp_roots=mcp_roots,
        design_zone=str(design_zone) if design_zone is not None else None,
    )


def _str_set(value: object) -> frozenset[str]:
    """Coerce a seed path collection to a ``frozenset[str]``; empty for ``None``.

    A bare string is treated as one path (not iterated character-by-character).
    """
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, (Sequence, frozenset, set)) and not isinstance(
        value, Mapping
    ):
        return frozenset(str(item) for item in value)
    return frozenset()


def _first_severe_finding(
    findings: Sequence[SeedFinding],
) -> SeedFinding | None:
    """The first SEVERE ``SS-*`` finding in ``findings`` (FR-016), or ``None``."""
    for finding in findings:
        if finding.is_severe():
            return finding
    return None


# Kept importable for downstream wiring symmetry with the OLB-06 seam.
__all__ = [
    "ADMISSION_REASONS",
    "REJECTION_REASONS",
    "SEED_INVALID",
    "EMPTY_REGISTRY",
    "SLUG_COLLISION",
    "UNRESOLVABLE_BLAST_RADIUS",
    "SEVERITY_SEVERE",
    "SeedFinding",
    "SeedValidatorPort",
    "SpawnResult",
    "SpawnPort",
    "AdmissionRejection",
    "AdmitDecision",
    "AdmittedHold",
    "RunRecord",
    "ReconciledFailure",
    "discover_candidates",
    "admission_gate",
    "admit_and_spawn",
    "admit_candidate",
]
