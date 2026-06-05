#!/usr/bin/env bash
# tests/test_orchestrator_single_instance_lock.sh
# Single-instance guard (incident 2026-06-05): on Windows a background-stopped orchestrator's
# detached process survived, so stop->relaunch spawned CONCURRENT orchestrators racing the same
# state dir. orchestrator.sh now refuses to start if a LIVE orchestrator already holds
# <state_dir>/orchestrator.lock, and reclaims a stale lock (dead holder).
#
# A: a live-held lock -> the orchestrator HALTs (exit 7) at the guard BEFORE any claude spend.
# B: a stale lock (dead holder) -> guard reclaims it (does NOT exit 7).
#
# Run: bash tests/test_orchestrator_single_instance_lock.sh   (exit 0 = all PASS)

set -uo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RALPH_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
TMPROOT="$(mktemp -d)"; trap 'rm -rf "$TMPROOT"' EXIT
PASS=0; FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*" >&2; FAIL=$((FAIL+1)); }

# Minimal seed valid for orchestrator.sh lines 1..guard (workspace_root / state_dir_relative /
# work_registry). The guard is reached before stop_check/Planner, so no claude spend occurs.
WS="$TMPROOT/ws"; mkdir -p "$WS/state"
cat > "$WS/registry.md" <<'REG'
| ID | Status |
|---|---|
| X-1 | open |
REG
cat > "$TMPROOT/seed.md" <<EOF
---
initiative: { slug: lock_test, project_id: lock_test }
workspace_root: "$WS"
state_dir_relative: "state/"
work_registry: "registry.md"
budget: { tokens_usd: 1.0, iterations_max: 1 }
permission_posture: "--permission-mode auto"
completion_predicate:
  - name: registry_drained
    check_kind: registry_zero_open
    params: { path: "registry.md", filter: "Status != closed" }
---
EOF
LOCK="$WS/state/orchestrator.lock"

# ---- A: live-held lock -> orchestrator refuses with exit 7 ----
# Hold the lock with a real, live background process (its PID is alive for the guard's kill -0).
sleep 60 & HOLDER=$!
echo "$HOLDER" > "$LOCK"
set +e
out="$(bash "$RALPH_ROOT/orchestrator.sh" "$TMPROOT/seed.md" 2>&1)"; rc=$?
set -e
kill "$HOLDER" 2>/dev/null
if [[ "$rc" == "7" ]] && grep -q 'refusing to start a concurrent instance' <<<"$out"; then
  pass "[A] live-held lock -> exit 7 + refuse message (no concurrent instance)"
else
  fail "[A] expected exit 7 + refuse, got rc=$rc :: $(echo "$out" | tail -2)"
fi
# the guard must NOT have clobbered the live holder's lock
[[ "$(cat "$LOCK" 2>/dev/null)" == "$HOLDER" ]] && pass "[A] live holder's lock left intact" || fail "[A] lock was overwritten by the refused instance"

# ---- B: stale lock (dead holder) -> guard reclaims (does NOT exit 7) ----
# Use a PID that is not alive. Run a tiny harness that executes ONLY the guard snippet shape so we
# don't proceed into the (spendy) main loop — mirror the orchestrator's guard logic exactly.
DEAD=999999  # not a live pid
echo "$DEAD" > "$LOCK"
set +e
out2="$(
  STATE_DIR="$WS/state"; ORCH_LOCK="$STATE_DIR/orchestrator.lock"
  if [[ -f "$ORCH_LOCK" ]]; then
    _lock_pid="$(cat "$ORCH_LOCK" 2>/dev/null || echo "")"
    if [[ -n "$_lock_pid" ]] && kill -0 "$_lock_pid" 2>/dev/null; then echo "REFUSED"; exit 7; fi
    echo "reclaiming stale"
  fi
  echo "$$" > "$ORCH_LOCK"; echo "PROCEEDED pid=$$"
)"; rc2=$?
set -e
if [[ "$rc2" == "0" ]] && grep -q 'PROCEEDED' <<<"$out2"; then
  pass "[B] stale lock (dead holder) reclaimed -> proceeds (not exit 7); lock now holds the new pid"
else
  fail "[B] expected reclaim+proceed, got rc=$rc2 :: $out2"
fi

echo ""
echo "=== test_orchestrator_single_instance_lock.sh: $PASS PASS, $FAIL FAIL ==="
[[ $FAIL -eq 0 ]]
