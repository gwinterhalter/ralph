"""Registry port for the Outer Loop Supervisor (OLB-01).

Defines the abstract seam between the mechanical supervision cycle
(OLB-01, ``supervisor/cycle.py``) and the concrete Project Registry
read/write layer (OLB-02). The cycle host depends only on this Protocol;
OLB-02 supplies the branch-DB implementation behind it.

NFR-006 sole-writer: the write methods declared here are owned and
implemented by OLB-02; the OLB-01 skeleton declares but never invokes them.
Resolved per gate ``olb01-registry-port-seam`` (option A).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

# A Registry row is a read-only column->value mapping. The concrete row model
# (the extended `projects` table shape, Spec v1.3 §5.2) is OLB-02's to define;
# the OLB-01 seam stays abstract over it.
RegistryRow = Mapping[str, object]


@runtime_checkable
class RegistryPort(Protocol):
    """Abstract read/write seam over the Project Registry (Spec v1.3 §5).

    Read methods are consumed by the supervision cycle's reconcile/admit
    steps (§4.4). Write methods are declared for OLB-02 to implement and are
    never called by the OLB-01 skeleton (NFR-005 mechanical, NFR-006
    sole-writer).
    """

    # --- Reads (the surface the skeleton's no-op hooks will later consume) ---

    def read_candidates(self) -> Sequence[RegistryRow]:
        """Return Candidate Projects (Spec §6 FR-015): seed + work registry on
        disk, no admitted-or-later lifecycle state. Empty when none."""
        ...

    def read_running(self) -> Sequence[RegistryRow]:
        """Return Projects in a ``running`` lifecycle state (Spec §5.4) — the
        reconcile step's read set. Empty when none."""
        ...

    # --- Writes (declared for OLB-02; never invoked by the OLB-01 skeleton) ---

    def set_lifecycle_state(self, project_id: str, state: str) -> None:
        """Persist a Project Lifecycle State transition (Spec §5.2 FR-001,
        §5.3 FR-008). OLB-02 implements; the OLB-01 skeleton never calls."""
        ...

    def record_run(self, project_id: str, run: RegistryRow) -> None:
        """Write a Run Registry row at spawn (Spec §5.4, §6.3). OLB-02
        implements; the OLB-01 skeleton never calls."""
        ...

    def update_run_status(self, project_id: str, status: str) -> None:
        """Reconcile a Run Registry row to a terminal status (Spec §5.4
        FR-011). OLB-02 implements; the OLB-01 skeleton never calls."""
        ...
