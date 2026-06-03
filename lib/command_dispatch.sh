#!/usr/bin/env bash
# lib/command_dispatch.sh — operator-command channel dispatcher (FUP-0815).
# Polled from orchestrator.sh main loop between iterations (after Consumer-close, before
# next iter's stop_check). Reads $STATE_DIR/commands/*.json, validates each via
# lib/validate_artefact.sh against the matching ralph/schemas/command_<type>.schema.json,
# dispatches by command_type case, writes a <command_id>.response.json sibling, archives
# the processed command to commands/.processed/.
#
# Public function: command_dispatch_run STATE_DIR SEED ITER_LAST
#
# Pause path (FUP-0797 BINDING): consults $STATE_DIR/state_snapshot.json.pending_gate;
# if non-null, response is `deferred_pending_gate_in_flight` (pause is NEVER honored
# while a Consumer-confirm cycle is in flight). Otherwise writes
# $STATE_DIR/pause_requested.flag (consumed by orchestrator.sh main loop top).
#
# Bump_budget path: writes $STATE_DIR/budget_override.json (read by run_claude_json on
# next pre-call check; effective cap = max(seed.budget.tokens_usd, $override)).
#
# Query_register_state path: READ-ONLY. Produces a register-vs-predicates snapshot to
# the response.json. Touches no substrate.
set -euo pipefail

SCRIPT_DIR_CD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source the canonical YAML-frontmatter accessor (Initiative_Orchestrator_Spec §8.5).
# Idempotent: orchestrator.sh also sources lib/seed.sh; standalone test invocations need
# read_seed_field too so we source defensively. lib/seed.sh sets -euo pipefail (matches).
# shellcheck source=lib/seed.sh
source "$SCRIPT_DIR_CD/seed.sh"

# Write a response.json sibling for a command + archive the processed command.
# Args: <command_file> <status> <details_json>
_command_dispatch_respond_and_archive() {
  local cmd_file="$1" status="$2" details="$3"
  local cmd_id base resp_file proc_dir
  cmd_id="$(jq -r '.command_id // "unknown"' "$cmd_file" 2>/dev/null || echo "unknown")"
  base="$(basename "$cmd_file" .json)"
  resp_file="$(dirname "$cmd_file")/${base}.response.json"
  proc_dir="$(dirname "$cmd_file")/.processed"
  mkdir -p "$proc_dir"
  jq -nc --arg id "$cmd_id" --arg s "$status" --argjson d "$details" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{command_id:$id, status:$s, responded_at:$ts, details:$d}' > "$resp_file"
  # FUP-0842: operator_command event — one per dispatched command at an iteration boundary.
  # command_type read from the file (still present pre-mv); result = the dispatch status. state_dir
  # derived from the commands/ path; iteration from the exported EVENT_ITER. §6.3 best-effort.
  if command -v emit_event >/dev/null 2>&1; then
    local oc_state oc_ctype
    oc_state="$(dirname "$(dirname "$cmd_file")")"
    oc_ctype="$(jq -r '.command_type // "unknown"' "$cmd_file" 2>/dev/null || echo "unknown")"
    emit_event "$oc_state" "${EVENT_PROJECT_ID:-}" "${EVENT_SLUG:-}" "${EVENT_ITER:-0}" "orchestrator" "operator_command" "" "" "" \
      "$(jq -nc --arg c "$oc_ctype" --arg r "$status" '{command:$c, result:$r}')"
  fi
  mv "$cmd_file" "$proc_dir/"
}

# Dispatch a single command file.
# Args: <state_dir> <seed> <iter_last> <command_file>
_command_dispatch_one() {
  local state_dir="$1" seed="$2" iter_last="$3" cmd_file="$4"
  local cmd_type schema_path validate_helper="$SCRIPT_DIR_CD/validate_artefact.sh"
  local ralph_root pending_gate new_cap effective_cap budget_override budget_cap
  local current_spend registry_path work_registry snapshot

  cmd_type="$(jq -r '.command_type // empty' "$cmd_file" 2>/dev/null || echo "")"
  ralph_root="$(cd "$SCRIPT_DIR_CD/.." && pwd)"

  case "$cmd_type" in
    pause|bump_budget|query_register_state)
      schema_path="$ralph_root/schemas/command_${cmd_type}.schema.json"
      ;;
    *)
      _command_dispatch_respond_and_archive "$cmd_file" "unknown_command_type" \
        "$(jq -nc --arg t "${cmd_type:-<empty>}" '{received_command_type:$t, supported:["pause","bump_budget","query_register_state"]}')"
      return 0
      ;;
  esac

  if ! bash "$validate_helper" "$schema_path" "$cmd_file" 2>/tmp/_cmd_validate_err; then
    _command_dispatch_respond_and_archive "$cmd_file" "schema_validation_failed" \
      "$(jq -nc --arg err "$(cat /tmp/_cmd_validate_err 2>/dev/null || echo unknown)" --arg s "$schema_path" '{schema:$s, validator_stderr:$err}')"
    rm -f /tmp/_cmd_validate_err
    return 0
  fi
  rm -f /tmp/_cmd_validate_err

  case "$cmd_type" in
    pause)
      pending_gate="$(jq -r '.pending_gate // empty' "$state_dir/state_snapshot.json" 2>/dev/null || echo "")"
      if [[ -n "$pending_gate" && "$pending_gate" != "null" ]]; then
        # FUP-0797 BINDING: pause NEVER bypasses an in-flight Consumer-confirm cycle.
        _command_dispatch_respond_and_archive "$cmd_file" "deferred_pending_gate_in_flight" \
          "$(jq -nc --argjson pg "$pending_gate" '{pending_gate:$pg, message:"pause will be honored after current gate is resolved; re-issue \\btw pause once gate clears."}')"
      else
        touch "$state_dir/pause_requested.flag"
        _command_dispatch_respond_and_archive "$cmd_file" "pause_honored_at_iter_boundary" \
          "$(jq -nc --arg it "$iter_last" '{last_completed_iteration:$it, message:"orchestrator will clean-exit at next main-loop poll."}')"
      fi
      ;;
    bump_budget)
      new_cap="$(jq -r '.new_cap_usd' "$cmd_file")"
      cat > "$state_dir/budget_override.json" <<EOF
{"budget_cap_usd": $new_cap, "set_by": "command_dispatch", "command_id": "$(jq -r '.command_id' "$cmd_file")", "ts": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF
      _command_dispatch_respond_and_archive "$cmd_file" "budget_override_written" \
        "$(jq -nc --argjson cap "$new_cap" '{new_cap_usd:$cap, effective_on:"next run_claude_json pre-call check"}')"
      ;;
    query_register_state)
      # READ-ONLY snapshot — no substrate mutation.
      # Seed is YAML-frontmatter markdown; use read_seed_field (lib/seed.sh) not raw jq.
      budget_cap="$(read_seed_field "$seed" .budget.tokens_usd 2>/dev/null || echo "0")"
      [[ -z "$budget_cap" || "$budget_cap" == "null" ]] && budget_cap="0"
      current_spend="$(jq -r '.total_spend_usd // 0' "$state_dir/spend.json" 2>/dev/null || echo "0")"
      if [[ -f "$state_dir/budget_override.json" ]]; then
        budget_override="$(jq -r '.budget_cap_usd' "$state_dir/budget_override.json")"
      else
        budget_override="null"
      fi
      work_registry="$(read_seed_field "$seed" .work_registry 2>/dev/null || echo "")"
      [[ "$work_registry" == "null" ]] && work_registry=""
      registry_path=""
      if [[ -n "$work_registry" ]]; then
        local ws_root
        ws_root="$(read_seed_field "$seed" .workspace_root 2>/dev/null || echo "")"
        [[ "$ws_root" == "null" ]] && ws_root=""
        [[ -n "$ws_root" ]] && registry_path="${ws_root%/}/$work_registry"
      fi
      pending_gate="$(jq -r '.pending_gate // null' "$state_dir/state_snapshot.json" 2>/dev/null || echo "null")"
      snapshot="$(jq -nc \
        --arg it "$iter_last" \
        --argjson cap "${budget_cap:-0}" \
        --argjson spent "${current_spend:-0}" \
        --argjson ovr "${budget_override:-null}" \
        --arg reg "$registry_path" \
        --argjson pg "$pending_gate" \
        '{last_completed_iteration:$it, budget:{cap_usd:$cap, spent_usd:$spent, override_usd:$ovr}, work_registry_path:$reg, pending_gate:$pg, snapshot_taken_at:(now | todate)}')"
      _command_dispatch_respond_and_archive "$cmd_file" "register_state_snapshot" "$snapshot"
      ;;
  esac
}

# Public entry. Polled by orchestrator.sh between iterations.
# Args: <state_dir> <seed> <iter_last>
command_dispatch_run() {
  local state_dir="$1" seed="$2" iter_last="$3"
  local cmd_dir="$state_dir/commands"
  mkdir -p "$cmd_dir" "$cmd_dir/.processed"
  shopt -s nullglob
  local cmd_file
  for cmd_file in "$cmd_dir"/*.json; do
    # Skip response.json siblings (already-emitted responses, not commands).
    [[ "$cmd_file" == *.response.json ]] && continue
    _command_dispatch_one "$state_dir" "$seed" "$iter_last" "$cmd_file"
  done
  shopt -u nullglob
}
