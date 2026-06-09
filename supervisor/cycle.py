"""Supervision cycle host for the Outer Loop Supervisor (OLB-01).

``SupervisionCycle.run_once()`` executes one pass of the six Spec v1.3 §4.4
supervision steps — reconcile -> admit -> schedule -> attend -> guard ->
learn — in order, each delegating to a policy hook.

NFR-004 single-host single-process: one cycle host, no concurrency.
NFR-005 mechanical supervisor: the cycle host embeds no per-initiative
reasoning; every step delegates to its owning component behind the same
signature.

OLB-11 (C3 fleet scheduling) fills the step-3 Schedule and step-4 Attend hooks
through the additive collaborator bundles in ``supervisor/cycle_wiring.py``
(gate ``olb11-c3-cycle-wiring-scope`` option A — additive, no signature
reshaped). OLB-14 (C4 anomaly drills) additively fills the step-5 Guard hook the
same way (gate ``olb14-c4-guard-wiring-scope`` option A): the OLB-12 Cost
Circuit-Breaker + OLB-13 Repair-Auto-OK Policy + OLB-06 FR-038 trip composed
behind an unchanged ``_guard()`` signature. The collaborators carry defaults of
``None``, so a cycle constructed without them stays the OLB-01 mechanical no-op
(every hook runs, no registry write is issued); the step-6 Learn hook awaits its
owning component (OLB-15).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from supervisor.cycle_wiring import (
    run_attend_step,
    run_guard_step,
    run_learn_step,
    run_reconcile_step,
    run_schedule_fill_step,
)

if TYPE_CHECKING:
    from supervisor.cycle_wiring import (
        AttendConfig,
        GuardConfig,
        LearnConfig,
        ReconcileConfig,
        ScheduleConfig,
    )
    from supervisor.ports import RegistryPort


class SupervisionCycle:
    """Mechanical host for the §4.4 supervision cycle.

    Depends on an injected :class:`~supervisor.ports.RegistryPort` (the OLB-01 ->
    OLB-02 seam, resolved per gate ``olb01-registry-port-seam`` option A), plus the
    optional OLB-11 Schedule / Attend collaborator bundles. Holds no per-initiative
    state; issues a registry write only through the configured policy hooks (the
    skeleton, and every unconfigured hook, writes nothing).
    """

    def __init__(
        self,
        registry: RegistryPort,
        *,
        schedule_config: ScheduleConfig | None = None,
        attend_config: AttendConfig | None = None,
        guard_config: GuardConfig | None = None,
        reconcile_config: ReconcileConfig | None = None,
        learn_config: LearnConfig | None = None,
    ) -> None:
        self._registry = registry
        # OLB-11 / OLB-14 additive collaborators (gates olb11-c3-cycle-wiring-scope /
        # olb14-c4-guard-wiring-scope, both option A). Defaulted to None so existing
        # OLB-01/OLB-05/OLB-11 call sites — SupervisionCycle(registry[, schedule_config=
        # ..., attend_config=...]) — and their RegistryPort doubles stay valid: a None
        # config leaves that hook the OLB-01 mechanical no-op.
        self._schedule_config = schedule_config
        self._attend_config = attend_config
        self._guard_config = guard_config
        self._reconcile_config = reconcile_config
        self._learn_config = learn_config

    def run_once(self) -> None:
        """Execute one supervision cycle: the six §4.4 steps, in order.

        Over a zero-candidate registry this is a complete no-op pass — every
        hook runs, no registry write is issued, and nothing is raised
        (the OLB-01 predicate).
        """
        self._reconcile()
        self._admit()
        self._schedule()
        self._attend()
        self._guard()
        self._learn()

    # --- §4.4 policy hooks: empty stubs (NFR-005 mechanical). Each owning
    #     component (OLB-02 onward) supplies the real policy behind the same
    #     signature; the skeleton invokes them in order and does nothing else. ---

    def _reconcile(self) -> None:
        """§4.4(1) Reconcile — mark stalled/terminated running Runs (T1#1).

        When a :class:`~supervisor.cycle_wiring.ReconcileConfig` is configured,
        reaps orphaned (dead orchestrator PID -> ``failed``) and stalled (past the
        hang budget -> ``halted`` / ``paused_gate``) running Runs, releasing the
        active-run unique-index slot so the Project can be re-gated. Unconfigured,
        it is the OLB-01 no-op.
        """
        if self._reconcile_config is not None:
            run_reconcile_step(self._registry, self._reconcile_config)

    def _admit(self) -> None:
        """§4.4(2) Admit — gate Candidates, admit-and-spawn the passers.

        In the OLB-11 wiring the FR-015–021 Admission Pipeline is performed
        ATOMICALLY inside the Schedule step's dispatch (``run_schedule_step`` selects
        the next Candidate and calls ``admit_candidate`` — the only Candidate→running
        path, enforcing the FR-016 seed-validity / FR-019 ceiling / FR-020
        blast-radius preconditions). So this hook is an intentional structural
        pass-through in this architecture, NOT an un-implemented gap — splitting a
        second standalone admit-scan here would double-process the same Candidates.
        """

    def _schedule(self) -> None:
        """§4.4(3) Schedule — dispatch Projects up to the Concurrency Ceiling (OLB-11).

        When a :class:`~supervisor.cycle_wiring.ScheduleConfig` is configured, runs
        the OLB-09 scheduler -> OLB-07 admission composition via
        :func:`~supervisor.cycle_wiring.run_schedule_fill_step` — repeating the
        single-pass dispatch (priority order, the FR-019/FR-037 ceiling hold, FR-027
        dispatch idempotency, FR-026 gate-blocked skip without demotion) up to
        ``config.max_dispatches_per_cycle`` times so a cold fleet ramps to full
        concurrency in one cycle rather than one Project per ``--interval``. With the
        default ``max_dispatches_per_cycle == 1`` this is exactly the prior
        single-dispatch pass. Unconfigured, it is the OLB-01 no-op.
        """
        if self._schedule_config is not None:
            run_schedule_fill_step(self._registry, self._schedule_config)

    def _attend(self) -> None:
        """§4.4(4) Attend — route pending gate_human escalations (OLB-11).

        When an :class:`~supervisor.cycle_wiring.AttendConfig` is configured, intakes
        newly-raised escalations (FR-028) and plans operator notifications (FR-029–031)
        through the OLB-10 attention layer. Unconfigured, it is the OLB-01 no-op.
        """
        if self._attend_config is not None:
            run_attend_step(self._registry, self._attend_config)

    def _guard(self) -> None:
        """§4.4(5) Guard — enforce safety-gates continuously (OLB-14).

        When a :class:`~supervisor.cycle_wiring.GuardConfig` is configured, runs the
        OLB-12 Cost Circuit-Breaker over the running fleet (FR-039, per-Project FR-043
        isolation) and the OLB-13 Repair-Auto-OK Policy over detected stalls (FR-044/
        045/046): a reversible, in-scope, confidence-met repair is granted autonomously
        (FR-045, the Run continues) and every other anomaly is tripped to
        ``paused_safety`` with a top-tier escalation through the OLB-06 FR-038 seam
        (never a kill, the no-silent-kill invariant). Unconfigured, it is the OLB-01 no-op.
        """
        if self._guard_config is not None:
            run_guard_step(self._registry, self._guard_config)

    def _learn(self) -> None:
        """§4.4(6) Learn — periodically invoke the Run-Auditor (D1; FR-049–053).

        When a :class:`~supervisor.cycle_wiring.LearnConfig` is configured, runs the
        OLB-15 read-only cross-run audit over the supplied completed/failed Runs and
        hands the findings-only report to the sink (no registry write — FR-053).
        Unconfigured (or with no Runs to audit), it is the OLB-01 no-op.
        """
        if self._learn_config is not None:
            run_learn_step(self._learn_config)
