#!/usr/bin/env python3
"""Read-only OL supervisor FLEET status — reuses ``supervisor.registry`` (PROD_DB_URL).

Run from the ralph cwd:  ``python ops/supervisor_status.py``

Prints the fleet gate state, the DISPATCHABLE-NOW count (0 == safely gated), any in-flight
runs, attention debt, and cumulative all-time spend. It NEVER writes — pure observation, so it
respects the operator's no-run-without-approval gate. Safe to run any time, in any session.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from decimal import Decimal

# The ``supervisor`` package lives in the ralph root (this file's parent's parent). Running
# ``python ops/supervisor_status.py`` puts ops/ on sys.path, not ralph/ — so add the root
# explicitly and this works from any cwd / invocation form.
_RALPH_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RALPH_ROOT not in sys.path:
    sys.path.insert(0, _RALPH_ROOT)

try:
    from supervisor.registry import Registry
except ModuleNotFoundError as exc:  # supervisor package not found next to ops/
    sys.exit(f"import failed ({exc}); expected supervisor/ under {_RALPH_ROOT}")


def _val(row, key, default=""):
    """RegistryRow may be a Mapping or an attr-object — read either shape."""
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return getattr(row, key, default)


def main() -> int:
    try:
        reg = Registry.from_env()  # reads PROD_DB_URL; lazily imports psycopg
    except RuntimeError as exc:
        sys.exit(f"registry unavailable: {exc}")

    projects = list(reg.read_all_projects())
    by_state = Counter(str(_val(p, "lifecycle_state", "?")) for p in projects)
    candidates = list(reg.read_candidates())
    running = list(reg.read_running())
    try:
        admitted = list(reg.read_admitted())  # FR-019 ceiling-held; additive read
    except AttributeError:
        admitted = []
    try:
        cumulative = reg.read_cumulative_spend_usd()
    except Exception:  # best-effort — never let a spend read sink the whole status
        cumulative = Decimal("-1")

    line = "=" * 66
    print(line)
    print("OL SUPERVISOR - FLEET STATUS (read-only)")
    print(line)
    print(f"projects total: {len(projects)}")
    for state in sorted(by_state):
        print(f"  {state:<20} {by_state[state]}")
    print("-" * 66)

    disp = len(candidates)
    flag = "GATED - nothing will dispatch" if disp == 0 else f"** {disp} WILL DISPATCH next cycle **"
    print(f"DISPATCHABLE NOW (candidate): {disp}   -> {flag}")
    print(f"in-flight: running={len(running)}  admitted/held={len(admitted)}")
    for p in list(running) + list(admitted):
        print(f"    - {_val(p, 'project_id')}  ({_val(p, 'lifecycle_state')}, prio {_val(p, 'priority')})")

    debt = sum(int(_val(p, "attention_debt", 0) or 0) for p in projects)
    print(f"attention debt (sum): {debt}")
    if cumulative >= 0:
        print(f"cumulative spend all-time: ${cumulative:.2f}")
    else:
        print("cumulative spend all-time: (unavailable)")
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
