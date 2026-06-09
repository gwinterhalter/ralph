"""ABS-phase on-ramp (Fleet Analytics spec §3).

Provisions the ABS Phase 0 -> 1 -> 2 chain as a dependency-gated fleet so the Outer Loop runs it
end-to-end: three ``projects`` rows whose ``depends_on`` edges are exactly the Item 1 mechanism —
Phase 1 is held ``candidate`` until Phase 0 is ``complete``, Phase 2 until Phase 1 is. The on-ramp
wires the FLEET only; the ABS-Phase-{0,1,2} seeds + work_registries are ABS-side work
(``ABS_Phase0_Phase1_RL_Implementation_Recommendations_v1.0.md``) and are assumed to exist (D4).

Pure plan (:func:`abs_chain_plan` / :func:`render_chain_plan`); the provisioning write is
``Registry.upsert_project`` invoked by the control-panel ``onramp-abs --apply``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AbsPhase:
    """One ABS phase as a fleet project: id, relative folder, priority, prerequisite phase ids."""

    project_id: str
    folder_path: str
    priority: int
    depends_on: tuple[str, ...]


#: The three ABS phases. Priority descends along the chain (Phase 0 first) so that, once a
#: prerequisite completes and unblocks the next phase, the scheduler prefers it. ``folder_path`` is
#: a relative subdir resolved under the supervisor workspace root (the same convention every other
#: project row uses). The ``depends_on`` edges are the Item 1 cross-initiative gate.
_PHASE0 = AbsPhase("abs_phase0", "abs_phase0", 30, ())
_PHASE1 = AbsPhase("abs_phase1", "abs_phase1", 20, ("abs_phase0",))
_PHASE2 = AbsPhase("abs_phase2", "abs_phase2", 10, ("abs_phase1",))


def abs_chain_plan() -> tuple[AbsPhase, ...]:
    """The ABS Phase 0->1->2 chain plan (pure; deterministic)."""
    return (_PHASE0, _PHASE1, _PHASE2)


def render_chain_plan(phases: tuple[AbsPhase, ...]) -> str:
    """Render the on-ramp plan (pure; the dry-run view)."""
    lines = ["ABS on-ramp plan (dependency-gated chain):"]
    for phase in phases:
        deps = ", ".join(phase.depends_on) if phase.depends_on else "(none)"
        lines.append(
            f"  {phase.project_id}  priority={phase.priority}  folder={phase.folder_path}  "
            f"depends_on={deps}"
        )
    lines.append(
        "apply provisions these as `candidate` projects; Item 1 holds each phase until its "
        "prerequisite is `complete`."
    )
    return "\n".join(lines)


__all__ = ["AbsPhase", "abs_chain_plan", "render_chain_plan"]
