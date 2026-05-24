#!/usr/bin/env bash
# orchestrator.sh — Ralph-loop controller (Initiative_Orchestrator_Spec §13.1 verbatim shape).
# Adaptations (plan Step 14): source lib/seed.sh; resolve STATE_DIR to an absolute path
#   (WORKSPACE_ROOT/state_dir_relative); §6.3 resumability startup; §6.1 mkdir scaffolding;
#   $SCRIPT_DIR-relative hook paths; orchestrator.log appends.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/seed.sh
source "$SCRIPT_DIR/lib/seed.sh"

SEED="${1:?usage: orchestrator.sh <seed_path>}"

WORKSPACE_ROOT="$(read_seed_field "$SEED" .workspace_root)"
STATE_DIR_REL="$(read_seed_field "$SEED" .state_dir_relative)"
STATE_DIR="$WORKSPACE_ROOT/$STATE_DIR_REL"          # absolute; §6.1 layout root
WORK_REGISTRY="$(read_seed_field "$SEED" .work_registry)"

log() { mkdir -p "$STATE_DIR/logs"; printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$STATE_DIR/logs/orchestrator.log"; }

# next_iteration_index — canonical algorithm (§13.1 verbatim).
# Returns the next 4-digit iteration index by scanning iterations/NNNN/.
# Bootstrap (empty state dir or no iterations subdir) returns "0001".
next_iteration_index() {
  local iter_dir="$1/iterations"
  if [[ ! -d "$iter_dir" ]]; then echo "0001"; return; fi
  local max
  # shellcheck disable=SC2010  # §13.1 verbatim algorithm; dir names are 4-digit numerics (no special chars)
  max=$(ls -1 "$iter_dir" 2>/dev/null | grep -E '^[0-9]{4}$' | sort -n | tail -1)
  if [[ -z "$max" ]]; then echo "0001"; else printf "%04d\n" $((10#$max + 1)); fi
}

# §6.3 resumability rule / bootstrap.
registry_hash() { [[ -f "$1" ]] && sha256sum "$1" | cut -d' ' -f1 || echo "MISSING"; }

if [[ -f "$STATE_DIR/state_snapshot.json" ]]; then
  log "RESUME: state_snapshot.json found"
  snap_hash="$(jq -r '.work_registry_hash_at_snapshot // empty' "$STATE_DIR/state_snapshot.json")"
  cur_hash="$(registry_hash "$WORK_REGISTRY")"
  if [[ -n "$snap_hash" && "$snap_hash" != "$cur_hash" ]]; then
    log "HALT: work_registry_hash mismatch (snapshot=$snap_hash current=$cur_hash) — registry edited outside orchestrator (§6.3 step 2)"
    echo "HALT: REGISTRY_HASH_MISMATCH" >&2
    exit 3
  fi
  pending_gate="$(jq -r '.pending_gate // empty' "$STATE_DIR/state_snapshot.json")"
  if [[ -n "$pending_gate" && "$pending_gate" != "null" ]]; then
    log "RESUME: pending_gate present — gate-resolution path (§6.3 step 3)"
  fi
else
  log "BOOTSTRAP: no snapshot — initialising state dir"
  mkdir -p "$STATE_DIR/iterations" "$STATE_DIR/gates" "$STATE_DIR/escalations" "$STATE_DIR/logs"
  [[ -f "$STATE_DIR/seed.md" ]] || cp "$SEED" "$STATE_DIR/seed.md"   # seed written once, never modified (§6.1)
fi

# Main role-call loop (§13.1 verbatim body).
while true; do
  if "$SCRIPT_DIR/hooks/stop_check.sh" "$SEED" "$STATE_DIR"; then
    log "INITIATIVE_COMPLETE: all completion_predicate[] passed"
    echo "INITIATIVE_COMPLETE"
    exit 0
  fi
  ITER=$(next_iteration_index "$STATE_DIR")
  ITER_DIR="$STATE_DIR/iterations/$ITER"
  mkdir -p "$ITER_DIR"
  log "ITERATION $ITER begin"

  # Planner Role Call
  claude -p "/rl-initiative-planner $STATE_DIR $ITER_DIR" \
    > "$ITER_DIR/planner.stdout" 2> "$ITER_DIR/planner.stderr"

  # Plan review (inner loop; bash-hook-orchestrated, §13.2)
  "$SCRIPT_DIR/hooks/plan_review.sh" "$ITER_DIR/session_plan_${ITER}.md"

  # Executor + gate loop
  "$SCRIPT_DIR/hooks/execute_with_gates.sh" "$SEED" "$ITER_DIR"

  # Consumer Role Call
  claude -p "/rl-iteration-consumer $STATE_DIR $ITER_DIR"

  # Budget check
  "$SCRIPT_DIR/hooks/budget_check.sh" "$SEED" "$STATE_DIR" || { log "BUDGET_EXHAUSTED"; exit 2; }
  log "ITERATION $ITER end"
done
