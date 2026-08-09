"""Per-run filesystem signals for the §4.4(1) Reconcile step (production wiring).

Two live, per-run signals the pure reconcile core (:mod:`supervisor.reconcile`)
consumes via injected probes — both read a single run's own state dir, derived from
its recorded ``seed_path`` (``<seed_parent>/state``), and are evaluated FRESH on every
reconcile pass (no startup snapshot):

* :func:`latest_progress_ts` — the newest progress instant from the run's OWN
  ``logs/events.jsonl`` (folds the FUP-0830 ``phase_complete`` + per-iteration
  progress events). This makes stall detection *progress-based* (time since last
  progress) rather than *wall-clock-since-spawn*: the prior wiring read one global
  log once at supervisor startup, so a long-but-live multi-iteration Run was reaped
  the moment it crossed the hang budget measured from spawn.

* :func:`has_pending_gate` — True iff the run persisted a human gate to
  ``state/escalations/`` that has no matching ``gate_response`` yet. An orchestrator
  that hits a needs-review gate persists it and EXITS to await the operator; without
  this signal the Reconcile step sees the dead PID and mislabels the run ``failed``
  instead of ``paused_gate`` (which both hides the gate from the operator surface and
  blocks the gate_response→resume path).

Pure over the filesystem: no DB, no wall-clock. Never raises (a missing/garbled state
dir yields ``None`` / ``False``).
"""

from __future__ import annotations

import re
from pathlib import Path

from supervisor.heartbeats import read_heartbeats_from_log

#: Run state dir + event log, relative to the seed file's parent.
_STATE_DIR_NAME = "state"
_EVENTS_REL = ("logs", "events.jsonl")
_ESCALATIONS_REL = "escalations"
_ITERATIONS_REL = "iterations"

#: gate_request_<iter>_<gate>.json / gate_response_<iter>_<gate>.json
_GATE_REQ_RE = re.compile(r"^gate_request_(\d+_\d+)\.json$")
_GATE_RESP_RE = re.compile(r"^gate_response_(\d+_\d+)\.json$")


def _state_dir(seed_path: str | None) -> Path | None:
    if not isinstance(seed_path, str) or not seed_path:
        return None
    try:
        return Path(seed_path).parent / _STATE_DIR_NAME
    except (OSError, ValueError):
        return None


def latest_progress_ts(seed_path: str | None) -> str | None:
    """ISO-8601 timestamp of the run's most recent progress event, or ``None``.

    Reads ``<seed_parent>/state/logs/events.jsonl`` fresh and returns the newest
    heartbeat-event instant (see :data:`supervisor.heartbeats.HEARTBEAT_EVENT_TYPES`).
    ``None`` when the log is absent/empty or carries no progress event — the caller
    falls back to ``spawned_at``.
    """
    state = _state_dir(seed_path)
    if state is None:
        return None
    events_path = state.joinpath(*_EVENTS_REL)
    try:
        if not events_path.is_file():
            return None
        beats = read_heartbeats_from_log(events_path)
    except OSError:
        return None
    if not beats:
        return None
    return max(beats.values()).isoformat()


def _response_indices(state: Path) -> set[str]:
    """All gate_response indices (``<iter>_<gate>``) present anywhere under the state dir."""
    found: set[str] = set()
    for sub in (state / _ESCALATIONS_REL, *(state / _ITERATIONS_REL).glob("*")):
        try:
            if not sub.is_dir():
                continue
            for entry in sub.iterdir():
                m = _GATE_RESP_RE.match(entry.name)
                if m:
                    found.add(m.group(1))
        except OSError:
            continue
    return found


def has_pending_gate(seed_path: str | None) -> bool:
    """True iff the run has an escalated gate_request with no matching gate_response.

    A needs-review gate the orchestrator escalated lands in ``state/escalations/`` as
    ``gate_request_<iter>_<gate>.json``; it is resolved when a ``gate_response`` with
    the same ``<iter>_<gate>`` index appears (in ``escalations/`` or the matching
    ``iterations/<iter>/``). Any request without its response → a pending gate.
    """
    state = _state_dir(seed_path)
    if state is None:
        return False
    esc = state / _ESCALATIONS_REL
    try:
        if not esc.is_dir():
            return False
        requested = {
            m.group(1)
            for entry in esc.iterdir()
            if (m := _GATE_REQ_RE.match(entry.name))
        }
    except OSError:
        return False
    if not requested:
        return False
    return bool(requested - _response_indices(state))


__all__ = ["has_pending_gate", "latest_progress_ts"]
