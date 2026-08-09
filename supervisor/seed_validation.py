"""Live seed-validation Port for the Outer Loop Supervisor (OLB-08 / C2).

The live ``SeedValidatorPort`` impl OLB-07 deferred: returns the cf-seed-reviewer
``SS-*`` finding set for a Candidate's seed (Spec v1.3 §6.2 FR-016, §14 FR-054). A
finding at :data:`~supervisor.admission.SEVERITY_SEVERE` blocks ``candidate ->
admitted`` in :func:`~supervisor.admission.admission_gate`; a clean seed (no
SEVERE finding) lets admission proceed.

OLB-07 shipped the DB-free admission decision layer behind the injectable
:class:`~supervisor.admission.SeedValidatorPort` Protocol and drove it with a fake;
this module implements the live structural ``SS-*`` check behind that seam. For the
build-controlled, known-clean ``oltest_*`` C2 seed a thin structural check is
sufficient; it MUST return a real ``Sequence[SeedFinding]`` matching the Protocol.

It edits no closed seam: it implements the existing Protocol and is injected into
the admission pipeline unchanged.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from supervisor.admission import SEVERITY_SEVERE, SeedFinding
from supervisor.ports import RegistryRow

# cf-seed-reviewer SS-* rule ids exercised by the live structural check (the subset
# of the rule set that is verifiable without an LLM pass — a seed that fails any of
# these cannot spawn a runnable orchestrator, so each is SEVERE).
SS_SEED_MISSING = "SS-001"
SS_NO_FRONTMATTER = "SS-002"
SS_MISSING_REQUIRED_FIELD = "SS-003"

#: Frontmatter keys a runnable seed MUST declare (a subset of the schema-1.4
#: contract sufficient to spawn an orchestrator Run; cf-seed-reviewer SS-003). The
#: orchestrator reads each of these before its first role call (orchestrator.sh).
REQUIRED_SEED_KEYS: tuple[str, ...] = (
    "workspace_root",
    "state_dir_relative",
    "work_registry",
    "completion_predicate",
)

# The YAML frontmatter fence (the first two lines equal to `---`, matching the
# lib/seed.sh awk delimiter contract).
_FRONTMATTER_FENCE = "---"


class SeedReviewValidator:
    """Live :class:`~supervisor.admission.SeedValidatorPort` — structural SS-* check.

    Reads the Candidate's seed file (``candidate['seed_path']``) and returns the
    ``SS-*`` findings: a SEVERE finding for a missing seed, missing YAML
    frontmatter, or any absent required frontmatter key. Returns an empty sequence
    for a well-formed seed (admission proceeds). Stateless and DB-free.
    """

    def validate_seed(self, candidate: RegistryRow) -> Sequence[SeedFinding]:
        """Return the ``SS-*`` findings for ``candidate``'s seed; empty when clean."""
        seed_path = str(candidate.get("seed_path", "")).strip()
        if not seed_path:
            return [
                SeedFinding(
                    code=SS_SEED_MISSING,
                    severity=SEVERITY_SEVERE,
                    detail="candidate declares no seed_path",
                )
            ]
        seed = Path(seed_path)
        if not seed.is_file():
            return [
                SeedFinding(
                    code=SS_SEED_MISSING,
                    severity=SEVERITY_SEVERE,
                    detail=f"seed file not found: {seed}",
                )
            ]
        frontmatter = _extract_frontmatter(seed.read_text(encoding="utf-8"))
        if frontmatter is None:
            return [
                SeedFinding(
                    code=SS_NO_FRONTMATTER,
                    severity=SEVERITY_SEVERE,
                    detail=f"seed declares no YAML frontmatter: {seed}",
                )
            ]
        return [
            SeedFinding(
                code=SS_MISSING_REQUIRED_FIELD,
                severity=SEVERITY_SEVERE,
                detail=f"seed frontmatter missing required key {key!r}",
            )
            for key in REQUIRED_SEED_KEYS
            if not _declares_top_level_key(frontmatter, key)
        ]


def _extract_frontmatter(text: str) -> list[str] | None:
    """The lines between the first two ``---`` fences, or ``None`` when absent.

    Mirrors the lib/seed.sh awk delimiter contract: the frontmatter is the block
    between the first and second fence line; the markdown body is ignored.
    """
    lines = text.splitlines()
    fences = [i for i, line in enumerate(lines) if line.strip() == _FRONTMATTER_FENCE]
    if len(fences) < 2:
        return None
    return lines[fences[0] + 1 : fences[1]]


def _declares_top_level_key(frontmatter: Sequence[str], key: str) -> bool:
    """True iff a top-level (unindented) ``key:`` line appears in ``frontmatter``."""
    prefix = f"{key}:"
    return any(line.startswith(prefix) for line in frontmatter)


__all__ = [
    "REQUIRED_SEED_KEYS",
    "SS_MISSING_REQUIRED_FIELD",
    "SS_NO_FRONTMATTER",
    "SS_SEED_MISSING",
    "SeedReviewValidator",
]
