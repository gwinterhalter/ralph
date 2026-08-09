"""Run-Auditor — the Outer Loop's cross-run learning pass (OLB-15).

A pure, DB-free, no-I/O module (Spec v1.3 §12, FR-049..FR-053) that looks back
across completed Runs to surface patterns that should reduce future operator
involvement. It is the Outer-Loop analogue of cf-cc-result-reviewer and the
cf-*-auditor family: **read-only and findings-only** (§12.3 boundary, NFR-005 —
the mechanical Supervisor surfaces, it does not act). It reads supplied per-Run
auditable facts and emits findings; it never edits the artifacts it learns from.

All audit inputs are *supplied* by the caller — this module performs no database
read, no ``state\\`` tree read, no ``supervisor.registry`` import, no ``RegistryPort``
call, no file I/O, no wall-clock read, and no network access. The live assembly of
:class:`RunRecord` facts from the accumulated C2/C3/C4 state tree + the branch
``ralph_runs`` rows (via the read-only OLB-02/08a ``RegistryPort``), and the wiring
of :func:`run_audit_pass` into the ``supervisor/cycle.py`` §4.4 step-6 ``_learn``
hook (today an empty no-op stub), are the OLB-16 / C5 full-system pass — not this
iteration (gate ``olb15-run-auditor-build-scope`` = A, pure standalone module).
All thresholds are injected through an :class:`AuditConfig`; no seed is read here.

Spec mapping (§12.2):

* FR-049 Periodic cross-run pass — :func:`run_audit_pass` runs a read-only learning
  pass over the supplied ``complete`` / ``failed`` :class:`RunRecord`s, aggregating
  every finding type, and never mutates an input record.
* FR-050 Answerer-DSL candidate findings — :func:`derive_answerer_dsl_candidates`
  surfaces a gate pattern escalated to ``gate_human`` yet resolved with the SAME
  option across ``>= config.min_consistent_runs`` Runs; differing resolutions yield
  no finding.
* FR-051 Verification-binding findings — :func:`derive_binding_findings` surfaces a
  binding with a uniform record across ``>= config.min_consistent_runs`` Runs as
  ``over_verification`` (always passed) or ``binding_defect`` (always failed); a
  mixed record yields no finding.
* FR-052 Session-shape findings — :func:`derive_shape_findings` surfaces a shape
  that required reviewer revision in ``>= config.shape_revision_fraction`` of its
  uses as a shape-tuning candidate; below the fraction yields no finding.
* FR-053 Findings-only output — :func:`render_audit_report` is a PURE formatter
  producing the Outer-Loop-owned audit-output text; it returns the string only,
  writes no file, and the module exposes no function that edits the Answerer DSL,
  a verification binding, or a session shape. Adoption is a separate operator
  action routed (via each finding's ``routes_to``) to the operator + the right
  authoring skill (§12.3: cf-spec-writer for the DSL, cf-seed-producer for
  bindings, cf-session-plan-reviewer for shapes).

Seam alignment (read-only probed at iter-0029): the FR-049 "completed Runs" set
keys on the canonical terminal Run statuses ``complete`` / ``failed``
(``supervisor.transitions`` legal-state set; ``supervisor.run_lifecycle``
``RUN_STATUS_COMPLETE``); the FR-050 gate pattern is the ``gate_human`` escalation
the OLB-10 ``supervisor.attention.Escalation`` carries, threaded in here as a plain
``gate_id`` string and never re-derived. No closed seam is imported or called.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

#: Canonical terminal Run statuses the FR-049 "completed Runs" set keys on
#: (``supervisor.transitions`` legal-state set / ``run_lifecycle.RUN_STATUS_COMPLETE``).
#: Mirrored as a literal so this module imports no closed seam (gate scope = A).
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"complete", "failed"})

#: Adoption routes per §12.3 — every finding names the operator + the authoring skill
#: that would author the change; the Run-Auditor itself authors nothing (FR-053).
ROUTE_ANSWERER_DSL = "operator + cf-spec-writer"
ROUTE_BINDINGS = "operator + cf-seed-producer"
ROUTE_SHAPES = "operator + cf-session-plan-reviewer"
ROUTE_CORRECTIONS = "operator + cf-pytest"


# --------------------------------------------------------------------------- #
# Supplied audit inputs (the live assembly is OLB-16 / C5; here they are given).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GateEvent:
    """One gate occurrence within a Run (the FR-050 subject).

    ``gate_id`` is the pattern / cluster key (the ``gate_human`` escalation the OLB-10
    :class:`supervisor.attention.Escalation` carries). ``resolved_option`` is the
    option the operator chose, or ``None`` if the gate was not resolved.
    """

    gate_id: str
    escalated_to_gate_human: bool
    resolved_option: str | None = None


@dataclass(frozen=True)
class BindingOutcome:
    """One verification-binding result within a Run (the FR-051 subject)."""

    binding: str
    passed: bool


@dataclass(frozen=True)
class ShapeUsage:
    """One session-shape use within a Run (the FR-052 subject)."""

    shape: str
    required_reviewer_revision: bool


@dataclass(frozen=True)
class CorrectionRecord:
    """One L1-L4 correction attempt within a Run (the FR-correction subject).

    Supplied by the caller from the captured ``correction_attempt`` event stream; ``level`` is the
    patch level (``L1``..``L4``), ``run_id`` lets the deriver count DISTINCT Runs an item was
    corrected across (the recurrence signal)."""

    run_id: str
    project_slug: str
    item_id: str
    level: str
    attempt: int = 0


@dataclass(frozen=True)
class RunRecord:
    """One completed Run's auditable facts (read-only; never mutated by this module).

    ``status`` is a canonical terminal status (:data:`TERMINAL_RUN_STATUSES`). The
    nested collections are tuples so a record is structurally immutable — the FR-049
    no-mutation guarantee is then mechanical, not merely a convention.
    """

    run_id: str
    project_slug: str
    status: str
    gate_events: tuple[GateEvent, ...] = ()
    binding_outcomes: tuple[BindingOutcome, ...] = ()
    shape_usages: tuple[ShapeUsage, ...] = ()


@dataclass(frozen=True)
class AuditConfig:
    """Injected thresholds for the audit pass (no seed is read inside this module).

    ``min_consistent_runs`` is the FR-050 / FR-051 "configured number" of Runs a
    pattern must hold across; ``shape_revision_fraction`` is the FR-052 "configured
    fraction" of a shape's uses that must have required revision; ``cadence`` is the
    FR-049 "configured cadence or operator demand" marker, recorded for provenance
    only (this module does not schedule itself).
    """

    min_consistent_runs: int = 3
    shape_revision_fraction: float = 0.5
    cadence: str = "operator_demand"


# --------------------------------------------------------------------------- #
# Findings (the read-only output; adoption is a separate operator action).
# --------------------------------------------------------------------------- #
class FindingKind(Enum):
    """The category of an :class:`AuditFinding` (one per FR-050 / FR-051 / FR-052)."""

    ANSWERER_DSL_CANDIDATE = "answerer_dsl_candidate"
    VERIFICATION_BINDING = "verification_binding"
    SESSION_SHAPE = "session_shape"
    CORRECTION_PATTERN = "correction_pattern"


class BindingFindingClass(Enum):
    """The FR-051 sub-classification of a verification-binding finding."""

    OVER_VERIFICATION = "over_verification"
    BINDING_DEFECT = "binding_defect"


@dataclass(frozen=True)
class AuditFinding:
    """A single surfaced pattern. Carries no adopt/apply hook (FR-053 findings-only)."""

    kind: FindingKind
    subject: str
    evidence: str
    recommendation: str
    routes_to: str
    binding_class: BindingFindingClass | None = None


def _level_rank(level: str) -> int:
    """Numeric rank of a correction patch level (``L3`` -> 3); 0 when unparseable."""
    token = level.strip().upper().lstrip("L")
    return int(token) if token.isdigit() else 0


def derive_correction_findings(
    records: Sequence[CorrectionRecord], *, config: AuditConfig
) -> list[AuditFinding]:
    """FR-correction: work-items that repeatedly entered the correction loop across Runs.

    A work-item that required correction across ``>= config.min_consistent_runs`` DISTINCT Runs is a
    chronic-defect candidate (the item's test or spec is the likely real problem, not the build) —
    one finding naming the item, the Run count, and the deepest patch level reached. Fewer than the
    threshold yields none. Routes to the operator + cf-pytest (the test/code authoring surface)."""
    runs_by_item: dict[str, set[str]] = defaultdict(set)
    levels_by_item: dict[str, set[str]] = defaultdict(set)
    for record in records:
        if not record.item_id:
            continue
        runs_by_item[record.item_id].add(record.run_id)
        if record.level:
            levels_by_item[record.item_id].add(record.level)

    findings: list[AuditFinding] = []
    for item in sorted(runs_by_item):
        run_count = len(runs_by_item[item])
        if run_count < config.min_consistent_runs:
            continue
        max_level = max(levels_by_item[item], key=_level_rank, default="")
        level_note = f" (deepest patch {max_level})" if max_level else ""
        findings.append(
            AuditFinding(
                kind=FindingKind.CORRECTION_PATTERN,
                subject=item,
                evidence=f"entered the correction loop across {run_count} Runs{level_note}",
                recommendation=(
                    f"review work-item '{item}' for a chronic defect — it needed the L1-L4 "
                    f"correction loop across {run_count} Runs{level_note}; the test or spec is the "
                    f"likely real problem, not the build"
                ),
                routes_to=ROUTE_CORRECTIONS,
            )
        )
    return findings


def finding_key(finding: AuditFinding) -> str:
    """Stable identity for a finding: ``<kind>:<subject>[:<binding_class>]``.

    The same pattern recurring across audit passes maps to one key (one persisted row; one
    operator offer). Used by the DB capture (``run_audit_findings`` primary key) and the
    auto-feedback bridge (a freshly-seen key is a NEW learning to surface once)."""
    key = f"{finding.kind.value}:{finding.subject}"
    if finding.binding_class is not None:
        key = f"{key}:{finding.binding_class.value}"
    return key


@dataclass(frozen=True)
class RunAuditReport:
    """The FR-049 pass result: the findings plus the window / threshold metadata."""

    findings: tuple[AuditFinding, ...] = ()
    runs_audited: int = 0
    min_consistent_runs: int = 0
    shape_revision_fraction: float = 0.0


# --------------------------------------------------------------------------- #
# Shared "uniform across >= N Runs" predicate behind FR-050 / FR-051.
# --------------------------------------------------------------------------- #
def _uniform_value_across_runs(
    per_run_values: dict[str, set[str]], *, min_runs: int
) -> str | None:
    """Return the single value held uniformly across ``>= min_runs`` Runs, else ``None``.

    ``per_run_values`` maps a Run id to the set of values that Run contributed for one
    subject. A subject qualifies only when it appears in at least ``min_runs`` Runs AND
    every Run agreed on exactly the same single value (any within-Run conflict or any
    cross-Run disagreement disqualifies it — the conservative reading of "consistently").
    """
    if len(per_run_values) < min_runs:
        return None
    distinct: set[str] = set()
    for values in per_run_values.values():
        if len(values) != 1:
            return None
        distinct |= values
    if len(distinct) != 1:
        return None
    return next(iter(distinct))


# --------------------------------------------------------------------------- #
# FR-050 — Answerer-DSL candidate findings.
# --------------------------------------------------------------------------- #
def derive_answerer_dsl_candidates(
    runs: Sequence[RunRecord], *, config: AuditConfig
) -> list[AuditFinding]:
    """FR-050: gate patterns escalated to ``gate_human`` yet resolved identically.

    A ``gate_id`` escalated to ``gate_human`` AND resolved with the SAME option across
    ``>= config.min_consistent_runs`` Runs yields one candidate naming the pattern and
    its consistent resolution; a pattern resolved with differing options yields none.
    """
    options_by_gate: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for run in runs:
        for event in run.gate_events:
            if event.escalated_to_gate_human and event.resolved_option is not None:
                options_by_gate[event.gate_id][run.run_id].add(event.resolved_option)

    findings: list[AuditFinding] = []
    for gate_id in sorted(options_by_gate):
        resolution = _uniform_value_across_runs(
            options_by_gate[gate_id], min_runs=config.min_consistent_runs
        )
        if resolution is None:
            continue
        run_count = len(options_by_gate[gate_id])
        findings.append(
            AuditFinding(
                kind=FindingKind.ANSWERER_DSL_CANDIDATE,
                subject=gate_id,
                evidence=(
                    f"escalated to gate_human and resolved '{resolution}' "
                    f"across {run_count} Runs"
                ),
                recommendation=(
                    f"add an Answerer pre-classification DSL rule pre-resolving "
                    f"gate pattern '{gate_id}' to '{resolution}'"
                ),
                routes_to=ROUTE_ANSWERER_DSL,
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# FR-051 — Verification-binding findings.
# --------------------------------------------------------------------------- #
def derive_binding_findings(
    runs: Sequence[RunRecord], *, config: AuditConfig
) -> list[AuditFinding]:
    """FR-051: bindings with a uniform pass/fail record across Runs.

    A binding uniformly passing across ``>= config.min_consistent_runs`` Runs is an
    ``over_verification`` candidate; uniformly failing is a ``binding_defect`` candidate;
    a mixed pass/fail record yields no finding.
    """
    results_by_binding: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for run in runs:
        for outcome in run.binding_outcomes:
            token = "pass" if outcome.passed else "fail"
            results_by_binding[outcome.binding][run.run_id].add(token)

    findings: list[AuditFinding] = []
    for binding in sorted(results_by_binding):
        verdict = _uniform_value_across_runs(
            results_by_binding[binding], min_runs=config.min_consistent_runs
        )
        if verdict is None:
            continue
        run_count = len(results_by_binding[binding])
        if verdict == "pass":
            finding_class = BindingFindingClass.OVER_VERIFICATION
            evidence = f"passed in every one of {run_count} Runs"
            recommendation = (
                f"review verification binding '{binding}' for over-verification "
                f"(it never caught a defect across {run_count} Runs)"
            )
        else:
            finding_class = BindingFindingClass.BINDING_DEFECT
            evidence = f"failed in every one of {run_count} Runs"
            recommendation = (
                f"review verification binding '{binding}' for a binding defect "
                f"(it failed across {run_count} Runs)"
            )
        findings.append(
            AuditFinding(
                kind=FindingKind.VERIFICATION_BINDING,
                subject=binding,
                evidence=evidence,
                recommendation=recommendation,
                routes_to=ROUTE_BINDINGS,
                binding_class=finding_class,
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# FR-052 — Session-shape findings.
# --------------------------------------------------------------------------- #
def derive_shape_findings(
    runs: Sequence[RunRecord], *, config: AuditConfig
) -> list[AuditFinding]:
    """FR-052: shapes that required reviewer revision in a configured fraction of uses.

    A shape whose ``required_reviewer_revision`` uses are ``>= config.shape_revision_fraction``
    of its total uses yields a shape-tuning finding; below the fraction yields none.
    """
    total_uses: dict[str, int] = defaultdict(int)
    revision_uses: dict[str, int] = defaultdict(int)
    for run in runs:
        for usage in run.shape_usages:
            total_uses[usage.shape] += 1
            if usage.required_reviewer_revision:
                revision_uses[usage.shape] += 1

    findings: list[AuditFinding] = []
    for shape in sorted(total_uses):
        uses = total_uses[shape]
        revisions = revision_uses[shape]
        if uses == 0:
            continue
        fraction = revisions / uses
        if fraction < config.shape_revision_fraction:
            continue
        findings.append(
            AuditFinding(
                kind=FindingKind.SESSION_SHAPE,
                subject=shape,
                evidence=(
                    f"required reviewer revision in {revisions} of {uses} uses "
                    f"({fraction:.0%})"
                ),
                recommendation=(
                    f"tune session-shape '{shape}' — it needed cf-session-plan-reviewer "
                    f"revision in {fraction:.0%} of its uses"
                ),
                routes_to=ROUTE_SHAPES,
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# FR-049 — the read-only cross-run pass; FR-053 — the pure findings-only render.
# --------------------------------------------------------------------------- #
def run_audit_pass(
    runs: Sequence[RunRecord], *, config: AuditConfig
) -> RunAuditReport:
    """FR-049: the read-only cross-run learning pass over the supplied Runs.

    Aggregates every finding type (FR-050 / FR-051 / FR-052) over the supplied
    ``complete`` / ``failed`` :class:`RunRecord`s and returns a :class:`RunAuditReport`.
    Reads only the supplied inputs — no DB, no ``state\\`` tree, no ``RegistryPort`` call,
    no file, no wall-clock — and never mutates an input record (the records are
    structurally immutable).
    """
    findings: list[AuditFinding] = []
    findings.extend(derive_answerer_dsl_candidates(runs, config=config))
    findings.extend(derive_binding_findings(runs, config=config))
    findings.extend(derive_shape_findings(runs, config=config))
    return RunAuditReport(
        findings=tuple(findings),
        runs_audited=len(runs),
        min_consistent_runs=config.min_consistent_runs,
        shape_revision_fraction=config.shape_revision_fraction,
    )


def render_audit_report(report: RunAuditReport) -> str:
    """FR-053: a PURE formatter rendering the Outer-Loop-owned audit-output text.

    Returns the rendered string only. It writes no file, schedules nothing, and edits
    no Answerer DSL / verification binding / session shape — adoption is a separate
    operator action carried out via each finding's :attr:`AuditFinding.routes_to`.
    """
    lines: list[str] = [
        "# Run-Auditor report (read-only, findings-only — §12.3 / FR-053)",
        (
            f"runs_audited={report.runs_audited} "
            f"min_consistent_runs={report.min_consistent_runs} "
            f"shape_revision_fraction={report.shape_revision_fraction:.2f}"
        ),
        "",
    ]
    if not report.findings:
        lines.append("No findings.")
        return "\n".join(lines)

    lines.append(f"Findings ({len(report.findings)}):")
    for index, finding in enumerate(report.findings, start=1):
        kind = finding.kind.value
        if finding.binding_class is not None:
            kind = f"{kind}:{finding.binding_class.value}"
        lines.append(f"{index}. [{kind}] {finding.subject}")
        lines.append(f"   evidence: {finding.evidence}")
        lines.append(f"   recommendation: {finding.recommendation}")
        lines.append(f"   routes_to (adoption is operator-owned): {finding.routes_to}")
    return "\n".join(lines)
