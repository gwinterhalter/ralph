"""FR-013 live OS start-time probe wiring (supervisor.__main__).

The pure re-attach decision logic is covered in test_reattach.py with injected
start-times. This module verifies the *real* psutil-backed probe that production wires
into ``derive_reattach_decisions`` (replacing the former ``lambda _pid: None`` stub):
the canonical formatter, the live probe against this very process, the
process-gone/psutil-absent fallback, and — end to end — that the wired probe produces
the correct RE-ATTACH vs ORPHAN_REUSED verdict against a real live pid.
"""

from __future__ import annotations

import os

import pytest

from supervisor.__main__ import _pid_start_time, format_pid_start_time
from supervisor.reattach import (
    DECISION_ORPHAN_REUSED,
    DECISION_REATTACH,
    derive_reattach_decisions,
)

pytestmark = pytest.mark.unit


def test_format_is_stable_fixed_precision() -> None:
    # Fixed 6-dp form so the recorded/live comparison can't drift on float repr.
    assert format_pid_start_time(1_717_000_000.0) == "1717000000.000000"
    assert format_pid_start_time(123.4) == format_pid_start_time(123.4)


def test_probe_returns_live_start_time_for_this_process() -> None:
    psutil = pytest.importorskip("psutil")
    pid = os.getpid()
    expected = format_pid_start_time(psutil.Process(pid).create_time())
    assert _pid_start_time(pid) == expected


def test_probe_returns_none_for_a_gone_process() -> None:
    pytest.importorskip("psutil")
    # A pid that is essentially certainly not a live process -> psutil.Error -> None.
    assert _pid_start_time(2_000_000_000) is None


def test_wired_probe_disambiguates_a_reused_pid() -> None:
    """The real proof: the psutil probe, wired through derive_reattach_decisions,
    yields RE-ATTACH when the recorded start-time matches the live process and
    ORPHAN_REUSED when it does not (pid recycled to a different process)."""
    psutil = pytest.importorskip("psutil")
    pid = os.getpid()
    live = format_pid_start_time(psutil.Process(pid).create_time())

    matches = [{"project_id": "p", "orchestrator_pid": pid, "pid_start_time": live}]
    d = derive_reattach_decisions(
        matches, pid_alive=lambda _p: True, pid_start_time=_pid_start_time
    )
    assert d[0].decision == DECISION_REATTACH

    mismatch = [
        {"project_id": "p", "orchestrator_pid": pid, "pid_start_time": "0.000000"}
    ]
    d2 = derive_reattach_decisions(
        mismatch, pid_alive=lambda _p: True, pid_start_time=_pid_start_time
    )
    assert d2[0].decision == DECISION_ORPHAN_REUSED
