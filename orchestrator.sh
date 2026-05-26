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
# FUP-0720 cost instrumentation: read budget cap from seed; running spend in state dir.
BUDGET_CAP="$(read_seed_field "$SEED" .budget.tokens_usd)"
# FUP-0736: read declared iteration cap (enforced in outer loop; empty/null = unbounded).
ITER_MAX="$(read_seed_field "$SEED" .budget.iterations_max)"
RUNNING_SPEND_FILE="$STATE_DIR/spend.json"

log() { mkdir -p "$STATE_DIR/logs"; printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$STATE_DIR/logs/orchestrator.log"; }

# run_claude_json — wraps `claude -p` with --output-format json + --max-budget-usd;
# captures total_cost_usd into the running spend; HALTs orchestrator if cumulative
# spend exceeds cap. FUP-0720. Usage: run_claude_json <output_file> <claude_prompt>
run_claude_json() {
  local out_file="$1"; shift
  local current_spend remaining_budget call_cost new_total
  [[ -f "$RUNNING_SPEND_FILE" ]] || echo '{"total_spend_usd": 0.0}' > "$RUNNING_SPEND_FILE"
  current_spend="$(jq -r '.total_spend_usd' "$RUNNING_SPEND_FILE")"
  remaining_budget="$(jq -rn --argjson cap "$BUDGET_CAP" --argjson cur "$current_spend" '$cap - $cur')"
  if awk "BEGIN { exit !($remaining_budget <= 0) }"; then
    log "HALT: BUDGET_EXHAUSTED before next claude -p (spend=$current_spend cap=$BUDGET_CAP)"
    echo "HALT: BUDGET_EXHAUSTED" >&2; exit 2
  fi
  claude -p --output-format json --max-budget-usd "$remaining_budget" "$@" > "$out_file"
  call_cost="$(jq -r '.total_cost_usd // 0' "$out_file")"
  new_total="$(jq -rn --argjson cur "$current_spend" --argjson cc "$call_cost" '$cur + $cc')"
  jq --argjson nt "$new_total" '.total_spend_usd = $nt' "$RUNNING_SPEND_FILE" > "$RUNNING_SPEND_FILE.tmp" \
    && mv "$RUNNING_SPEND_FILE.tmp" "$RUNNING_SPEND_FILE"
  log "claude -p call_cost=$call_cost running_total=$new_total cap=$BUDGET_CAP"
}

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
  # Phase 4b P4-03(b): capture stop_check.sh exit code and branch all 4 cases
  # (0 = COMPLETE; 1 = continue; 2 = BUDGET_EXHAUSTED; ≥3 = HALT). Previously the
  # bare `if "$hook"` collapsed 1/2/3 into a single "not complete → continue" path.
  set +e
  "$SCRIPT_DIR/hooks/stop_check.sh" "$SEED" "$STATE_DIR"
  sc_rc=$?
  set -e
  case $sc_rc in
    0)
      log "INITIATIVE_COMPLETE: all completion_predicate[] passed"
      echo "INITIATIVE_COMPLETE"
      exit 0
      ;;
    1)
      : # continue iteration
      ;;
    2)
      log "BUDGET_EXHAUSTED: stop_check returned exit 2"
      echo "BUDGET_EXHAUSTED" >&2
      exit 2
      ;;
    *)
      log "HALT: stop_check returned exit $sc_rc (malformed predicate / error per §13.2)"
      echo "HALT: STOP_CHECK_ERROR (exit $sc_rc)" >&2
      exit 3
      ;;
  esac
  ITER=$(next_iteration_index "$STATE_DIR")
  if [[ -n "$ITER_MAX" && "$ITER_MAX" != "null" ]] && (( 10#$ITER > ITER_MAX )); then
    log "HALT: MAX_ITERATIONS_EXCEEDED (iter=$ITER max=$ITER_MAX)"
    echo "HALT: MAX_ITERATIONS_EXCEEDED" >&2
    exit 6
  fi
  ITER_DIR="$STATE_DIR/iterations/$ITER"
  mkdir -p "$ITER_DIR"
  log "ITERATION $ITER begin"

  # Planner Role Call (FUP-0720: --output-format json + --max-budget-usd via run_claude_json)
  run_claude_json "$ITER_DIR/planner.json" "/rl-initiative-planner $STATE_DIR $ITER_DIR" \
    2> "$ITER_DIR/planner.stderr"
  # Extract markdown result for any downstream consumer expecting the textual emission:
  jq -r '.result // empty' "$ITER_DIR/planner.json" > "$ITER_DIR/planner.stdout"

  # Plan review (inner loop; bash-hook-orchestrated, §13.2)
  "$SCRIPT_DIR/hooks/plan_review.sh" "$ITER_DIR/session_plan_${ITER}.md"

  # Phase 4b P4-03(b): capture execute_with_gates.sh exit code (0/1/2) and branch.
  # exit 0 = continue to Consumer; exit 1 = FAILED iteration + escalate gate_human
  # (loop continues per §12); exit 2 = read-only boundary violation → terminal HALT
  # per FR-017. Previously bare call under `set -e` collapsed 1/2 indistinguishably.
  set +e
  "$SCRIPT_DIR/hooks/execute_with_gates.sh" "$SEED" "$ITER_DIR"
  ewg_rc=$?
  set -e
  case $ewg_rc in
    0)
      : # success → fall through to Consumer
      ;;
    1)
      log "ITERATION $ITER FAILED (execute_with_gates exit 1 — see escalations/)"
      mkdir -p "$STATE_DIR/escalations"
      printf '{"iteration":"%s","classification":"gate_human","reason":"execute_with_gates_exit_1","ts":"%s"}\n' \
             "$ITER" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
             > "$STATE_DIR/escalations/iteration_${ITER}_failed.json"
      # Continue loop (do not exit) so the Planner can decide to retry / reframe.
      log "ITERATION $ITER end (FAILED, gate_human escalated)"
      continue
      ;;
    2)
      log "HALT: execute_with_gates exit 2 (read-only boundary violation per FR-017)"
      echo "HALT: READ_ONLY_BOUNDARY_VIOLATION (iteration $ITER)" >&2
      exit 3
      ;;
    *)
      log "HALT: execute_with_gates returned unexpected exit $ewg_rc"
      echo "HALT: EXECUTE_WITH_GATES_UNEXPECTED_EXIT (rc=$ewg_rc, iteration $ITER)" >&2
      exit 3
      ;;
  esac

  # Consumer Role Call (FUP-0720: --output-format json + --max-budget-usd via run_claude_json)
  run_claude_json "$ITER_DIR/consumer.json" "/rl-iteration-consumer $STATE_DIR $ITER_DIR"

  # Phase 4b P4-07: fail_counts ≥3 deterministic guard (additive to FR-013 Planner
  # self-escalation). The Consumer maintains $STATE_DIR/fail_counts.json (per §5.4 /
  # FR-012) — a JSON array of {item_id, count, last_failure_iteration, last_reason}.
  # The orchestrator only READS and GATES; the Consumer owns increments. Threshold = 3.
  FAIL_COUNTS_FILE="$STATE_DIR/fail_counts.json"
  if [[ -f "$FAIL_COUNTS_FILE" ]]; then
    triggered_item="$(jq -r 'map(select(.count >= 3)) | .[0].item_id // empty' "$FAIL_COUNTS_FILE" 2>/dev/null || echo "")"
    if [[ -n "$triggered_item" ]]; then
      log "HALT: fail_counts threshold ≥3 for item_id=$triggered_item (P4-07 deterministic guard; gate_human escalated)"
      mkdir -p "$STATE_DIR/escalations"
      printf '{"iteration":"%s","classification":"gate_human","reason":"fail_counts_threshold","item_id":"%s","ts":"%s"}\n' \
             "$ITER" "$triggered_item" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
             > "$STATE_DIR/escalations/fail_counts_threshold_${ITER}_${triggered_item}.json"
      echo "HALT: FAIL_COUNTS_THRESHOLD (item=$triggered_item, iteration=$ITER)" >&2
      exit 3
    fi
  fi

  # Budget check
  "$SCRIPT_DIR/hooks/budget_check.sh" "$SEED" "$STATE_DIR" || { log "BUDGET_EXHAUSTED"; exit 2; }
  log "ITERATION $ITER end"
done
