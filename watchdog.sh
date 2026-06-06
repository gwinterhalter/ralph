#!/usr/bin/env bash
# T2#3: keep the Outer Loop orchestrator alive across crashes.
#
# Runs orchestrator.sh in the FOREGROUND and inspects how it ended. A *clean*
# terminal — INITIATIVE_COMPLETE, an operator PAUSE, a §6 HALT / gate_human block,
# or a single-instance-lock refusal — stops the watchdog (no restart: the loop is
# done or deliberately waiting for the operator). An *unexpected* death (a non-zero
# exit with no terminal marker, i.e. a crash / OOM / kill) is restarted with a
# fixed backoff, up to MAX_RESTARTS, so a transient failure self-heals instead of
# leaving the fleet dark until a human notices.
#
# The orchestrator's own single-instance lock still prevents duplicate live loops;
# the watchdog only ever launches one at a time and waits for it.
#
# NOTE: an OS-integrated process supervisor (NSSM / systemd / Windows Task
# Scheduler) is the production form and is out of spec scope (§2.2). This is the
# portable shell heartbeat+restart loop that needs no privileged install.
#
# Usage: watchdog.sh <seed_path> [max_restarts=5] [backoff_seconds=10]
#   RALPH_ORCHESTRATOR overrides the launched orchestrator (test seam).

set -uo pipefail

SEED="${1:?usage: watchdog.sh <seed_path> [max_restarts] [backoff_seconds]}"
MAX_RESTARTS="${2:-5}"
BACKOFF="${3:-10}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCHESTRATOR="${RALPH_ORCHESTRATOR:-$SCRIPT_DIR/orchestrator.sh}"

# Resolve STATE_DIR exactly as orchestrator.sh does, to read the terminal marker.
# shellcheck source=/dev/null
if [[ -f "$SCRIPT_DIR/lib/seed.sh" ]]; then
  source "$SCRIPT_DIR/lib/seed.sh"
  WORKSPACE_ROOT="$(read_seed_field "$SEED" .workspace_root)"
  STATE_DIR_REL="$(read_seed_field "$SEED" .state_dir_relative)"
  STATE_DIR="$WORKSPACE_ROOT/$STATE_DIR_REL"
else
  STATE_DIR="${RALPH_STATE_DIR:-}"
fi
ORCH_LOG="$STATE_DIR/logs/orchestrator.log"

# lib/seed.sh runs `set -e`; the restart loop MUST survive the orchestrator's
# non-zero exits, so explicitly disable errexit before the loop (a crashing child
# is the normal case the watchdog handles, not a fatal error for the watchdog).
set +e

restarts=0
while :; do
  "$ORCHESTRATOR" "$SEED"
  rc=$?

  log_tail="$(tail -8 "$ORCH_LOG" 2>/dev/null || true)"
  if grep -q 'INITIATIVE_COMPLETE' <<<"$log_tail"; then
    echo "watchdog: INITIATIVE_COMPLETE — initiative done, stopping." >&2
    exit 0
  fi
  if grep -q 'PAUSED at iter' <<<"$log_tail"; then
    echo "watchdog: operator PAUSE honored — stopping (resume relaunches)." >&2
    exit 0
  fi
  if grep -qE 'BLOCKED on gate_human|HALT' <<<"$log_tail"; then
    echo "watchdog: HALT / gate_human block — stopping for operator action." >&2
    exit 0
  fi
  if [[ "$rc" -eq 7 ]]; then
    echo "watchdog: another orchestrator holds the lock (exit 7) — stopping." >&2
    exit 7
  fi
  if [[ "$rc" -eq 0 ]]; then
    echo "watchdog: orchestrator exited 0 with no terminal marker — stopping." >&2
    exit 0
  fi

  restarts=$((restarts + 1))
  if [[ "$restarts" -gt "$MAX_RESTARTS" ]]; then
    echo "watchdog: orchestrator crashed (rc=$rc); $MAX_RESTARTS restarts exhausted — giving up." >&2
    exit 1
  fi
  echo "watchdog: orchestrator crashed (rc=$rc); restart $restarts/$MAX_RESTARTS after ${BACKOFF}s." >&2
  sleep "$BACKOFF"
done
