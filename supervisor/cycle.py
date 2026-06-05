"""Supervision cycle host for the Outer Loop Supervisor (OLB-01).

``SupervisionCycle.run_once()`` executes one pass of the six Spec v1.3 §4.4
supervision steps — reconcile -> admit -> schedule -> attend -> guard ->
learn — in order, each delegating to an empty (no-op) policy hook.

NFR-004 single-host single-process: one cycle host, no concurrency.
NFR-005 mechanical supervisor: the cycle host embeds no per-initiative
reasoning; every step is a documented stub until its owning component
(OLB-02 onward) supplies the policy behind the same signature.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from supervisor.ports import RegistryPort


class SupervisionCycle:
    """Mechanical host for the §4.4 supervision cycle.

    Depends only on an injected :class:`~supervisor.ports.RegistryPort` (the
    OLB-01 -> OLB-02 seam, resolved per gate ``olb01-registry-port-seam``
    option A). Holds no per-initiative state and issues no registry write in
    the skeleton.
    """

    def __init__(self, registry: RegistryPort) -> None:
        self._registry = registry

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
        """§4.4(3) Schedule — select the next admitted Project to Dispatch. No-op stub."""

    def _attend(self) -> None:
        """§4.4(4) Attend — route pending gate_human escalations. No-op stub."""

    def _guard(self) -> None:
        """§4.4(5) Guard — enforce safety-gates continuously. No-op stub."""

    def _learn(self) -> None:
        """§4.4(6) Learn — periodically invoke the Run-Auditor. No-op stub."""
