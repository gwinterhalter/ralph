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
reshaped). The collaborators carry defaults of ``None``, so a cycle constructed
without them stays the OLB-01 mechanical no-op (every hook runs, no registry
write is issued); the remaining four hooks await their owning components.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from supervisor.cycle_wiring import run_attend_step, run_schedule_step

if TYPE_CHECKING:
    from supervisor.cycle_wiring import AttendConfig, ScheduleConfig
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
    ) -> None:
        self._registry = registry
        # OLB-11 additive collaborators (gate olb11-c3-cycle-wiring-scope option A).
        # Defaulted to None so existing OLB-01/OLB-05 call sites — SupervisionCycle(
        # registry) — and their RegistryPort doubles stay valid: a None config leaves
        # that hook the OLB-01 mechanical no-op.
        self._schedule_config = schedule_config
        self._attend_config = attend_config

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
        """§4.4(1) Reconcile — mark stalled/terminated running Runs. No-op stub."""

    def _admit(self) -> None:
        """§4.4(2) Admit — gate Candidates, admit-and-spawn the passers. No-op stub."""

    def _schedule(self) -> None:
        """§4.4(3) Schedule — select the next Project to Dispatch (OLB-11).

        When a :class:`~supervisor.cycle_wiring.ScheduleConfig` is configured, runs
        the OLB-09 scheduler -> OLB-07 admission composition for one pass (priority
        order, the FR-019/FR-037 ceiling hold, FR-027 dispatch idempotency, FR-026
        gate-blocked skip without demotion). Unconfigured, it is the OLB-01 no-op.
        """
        if self._schedule_config is not None:
            run_schedule_step(self._registry, self._schedule_config)

    def _attend(self) -> None:
        """§4.4(4) Attend — route pending gate_human escalations (OLB-11).

        When an :class:`~supervisor.cycle_wiring.AttendConfig` is configured, intakes
        newly-raised escalations (FR-028) and plans operator notifications (FR-029–031)
        through the OLB-10 attention layer. Unconfigured, it is the OLB-01 no-op.
        """
        if self._attend_config is not None:
            run_attend_step(self._registry, self._attend_config)

    def _guard(self) -> None:
        """§4.4(5) Guard — enforce safety-gates continuously. No-op stub."""

    def _learn(self) -> None:
        """§4.4(6) Learn — periodically invoke the Run-Auditor. No-op stub."""
