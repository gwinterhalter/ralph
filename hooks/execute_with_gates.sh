#!/usr/bin/env bash
# hooks/execute_with_gates.sh <seed_path> <iter_dir>   (Initiative_Orchestrator_Spec §13.2 + §10.3 + §10.5)
# Pre-execution gate broker (§10.3 Path A) + claude --print execution + §10.5 FR-019 post-execution gate.
# Exit 0 — execution report + JSON written; iteration ready for Consumer
# Exit 1 — execution failed (crash, hang, irregular termination, classifier denial, or native budget cap)
# Exit 2 — read-only boundary violation detected; orchestrator HALT
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/seed.sh
source "$SCRIPT_DIR/../lib/seed.sh"

SEED="${1:?usage: execute_with_gates.sh <seed_path> <iter_dir>}"
ITER_DIR="${2:?usage: execute_with_gates.sh <seed_path> <iter_dir>}"
ITER="$(basename "$ITER_DIR")"
PLAN_PATH="$ITER_DIR/session_plan_${ITER}.md"
RESULT_JSON="$ITER_DIR/execution_result_${ITER}.json"
MCP_CONFIG="$ITER_DIR/mcp_config.json"
STATE_DIR="$(dirname "$(dirname "$ITER_DIR")")"

# Phase 4a P4-01: generate mcp_config.json from seed.mcp_servers[] merged with optional
# per-iteration plan_block. Frontmatter extraction mirrors lib/seed.sh:21 awk delimiter logic
# (yq can't parse the full markdown body; only the YAML between the first two '---' lines).
# Output JSON shape conforms to `claude --mcp-config`: {"mcpServers": {<name>: {...}, ...}}.
generate_mcp_config() {
  local seed="$1"; local iter_dir="$2"
  local fm seed_block plan_block merged
  fm=$(awk '/^---[[:space:]]*$/{c++; next} c==1{print} c==2{exit}' "$seed")
  seed_block="$(printf '%s\n' "$fm" | yq -o=json eval '.mcp_servers' - | jq 'if .==null then [] else . end')"
  if [[ -f "$iter_dir/mcp_additions.json" ]]; then
    plan_block="$(jq '.' "$iter_dir/mcp_additions.json")"
  else
    plan_block='[]'
  fi
  merged="$(echo "$seed_block $plan_block" | jq -s 'add | map({(.name): {command, args, env}}) | add // {} | {"mcpServers": .}')"
  mkdir -p "$iter_dir"
  echo "$merged" > "$iter_dir/mcp_config.json"
  echo "generate_mcp_config: wrote $iter_dir/mcp_config.json" >&2
}
generate_mcp_config "$SEED" "$ITER_DIR"

# Phase 4a P4-06: validate generated mcp_config.json against schema (typo-catch).
if ! bash "$SCRIPT_DIR/../lib/validate_artefact.sh" "$SCRIPT_DIR/../schemas/mcp_config.schema.json" "$MCP_CONFIG"; then
  echo "execute_with_gates: mcp_config.json invalid against schemas/mcp_config.schema.json" >&2
  exit 1
fi

# Phase 4b P4-03(a): iteration-start mtime marker for post-exec read-only-write scan.
# The post-exec scan (after claude --print) uses `find -newer "$ITER_START_MARKER"` against
# each read_only_paths[] root to detect writes that occurred during this iteration.
ITER_START_MARKER="$ITER_DIR/.iter_start"
touch "$ITER_START_MARKER"

# §10.3 Path A pre-execution gate broker.
shopt -s nullglob
for req in "$ITER_DIR"/gate_request_"${ITER}"_*.json; do
  # Phase 4a P4-06: validate gate_request against schema before classifying (typo-catch).
  if ! bash "$SCRIPT_DIR/../lib/validate_artefact.sh" "$SCRIPT_DIR/../schemas/gate_request.schema.json" "$req"; then
    echo "execute_with_gates: gate_request invalid: $req" >&2
    exit 1
  fi
  # Classify per seed.gate_policy.pre_classification[] (pattern DSL §10.2).
  # Phase 4a P4-04: 3 forms with priority order — gate_id: > cluster: > contains:
  # First-match-wins; default to gate_dc if no entry matches.
  gate_id="$(jq -r '.gate_id // empty' "$req")"
  cls="gate_dc"
  pc_count="$(read_seed_field "$SEED" '.gate_policy.pre_classification | length')"
  for ((j=0; j<pc_count; j++)); do
    pat="$(read_seed_field "$SEED" ".gate_policy.pre_classification[$j].pattern")"
    if [[ "$pat" == gate_id:* && "$gate_id" == "${pat#gate_id:}" ]]; then
      cls="$(read_seed_field "$SEED" ".gate_policy.pre_classification[$j].class")"; break
    elif [[ "$pat" == cluster:* ]]; then
      cluster_value="$(jq -r '.cluster // empty' "$req")"
      if [[ "$cluster_value" == "${pat#cluster:}" ]]; then
        cls="$(read_seed_field "$SEED" ".gate_policy.pre_classification[$j].class")"; break
      fi
    elif [[ "$pat" == contains:* ]]; then
      q_text="$(jq -r '.question_text // empty' "$req")"
      substring="${pat#contains:}"
      if [[ "${q_text,,}" == *"${substring,,}"* ]]; then
        cls="$(read_seed_field "$SEED" ".gate_policy.pre_classification[$j].class")"; break
      fi
    fi
  done
  if [[ "$cls" == "gate_human" ]]; then
    mkdir -p "$STATE_DIR/escalations"
    cp "$req" "$STATE_DIR/escalations/$(basename "$req")"
    echo "execute_with_gates: gate_human escalation for $gate_id" >&2
    return 1 2>/dev/null || exit 1
  fi
  # gate_dc -> resolve via rl-operator-answerer; answer inlined into plan text by the answerer.
  claude -p "/rl-operator-answerer $req" > "$ITER_DIR/gate_response_$(basename "$req")"
done
shopt -u nullglob

# Execute (§5.2 canonical invocation).
PERMISSION_POSTURE="$(read_seed_field "$SEED" .permission_posture)"
PER_CALL_CAP="$(read_seed_field "$SEED" '.budget.per_call_usd_cap // .budget.tokens_usd')"
MAX_TURNS="$(read_seed_field "$SEED" '.budget.max_turns_per_call // 200')"
# shellcheck disable=SC2086
claude --print --output-format json \
       --permission-mode "$PERMISSION_POSTURE" \
       --strict-mcp-config --mcp-config "$MCP_CONFIG" \
       --max-budget-usd "$PER_CALL_CAP" --max-turns "$MAX_TURNS" \
       < "$PLAN_PATH" > "$RESULT_JSON"

# Phase 4a P4-06: validate execution_result.json against schema before §10.5 fires (typo-catch).
if ! bash "$SCRIPT_DIR/../lib/validate_artefact.sh" "$SCRIPT_DIR/../schemas/execution_result.schema.json" "$RESULT_JSON"; then
  echo "execute_with_gates: execution_result invalid against schemas/execution_result.schema.json — refusing to evaluate §10.5 on malformed output" >&2
  exit 1
fi

# Phase 4b P4-03(a): read-only boundary violation scan. Highest priority — exit 2
# precedes any exit-1 classification per §13.2 hook contract (orchestrator translates
# exit 2 to its own HALT per FR-017). Scans each read_only_paths[] root for files
# modified since $ITER_START_MARKER (touched before claude --print ran above).
ro_count="$(read_seed_field "$SEED" '.read_only_paths | length' 2>/dev/null || echo 0)"
for ((r=0; r<ro_count; r++)); do
  ro_path="$(read_seed_field "$SEED" ".read_only_paths[$r]")"
  if [[ -d "$ro_path" ]]; then
    modified="$(find "$ro_path" -type f -newer "$ITER_START_MARKER" 2>/dev/null | head -1)"
    if [[ -n "$modified" ]]; then
      echo "execute_with_gates: read-only boundary violation — write detected under $ro_path: $modified" >&2
      exit 2
    fi
  fi
done

# §10.5 FR-019 post-execution gate — Phase 4b P4-03(c) branches narrative classification.
# Both branches still exit 1 (per §13.2 only the read-only violation is exit 2); the
# classification + escalation artefact distinguishes auto_mode_denial vs irregular_termination
# vs the existing native-budget-cap (terminal_reason-driven) path.
TERMINAL_REASON="$(jq -r '.terminal_reason' "$RESULT_JSON")"
DENIALS_COUNT="$(jq -r '.permission_denials | length' "$RESULT_JSON")"
if [[ "$DENIALS_COUNT" -ne 0 ]]; then
  mkdir -p "$STATE_DIR/escalations"
  esc_file="$STATE_DIR/escalations/auto_mode_denial_${ITER}_$(date -u +%s).json"
  jq --arg iter "$ITER" '{iteration: $iter, classification: "auto_mode_denial", denials_count: (.permission_denials | length), permission_denials: .permission_denials, terminal_reason: .terminal_reason}' "$RESULT_JSON" > "$esc_file"
  echo "execute_with_gates: auto_mode_denial classification — escalation $esc_file (denials=$DENIALS_COUNT)" >&2
  NOTIFY="$(read_seed_field "$SEED" '.notification_channel' 2>/dev/null || echo "")"
  if [[ -n "$NOTIFY" && "$NOTIFY" != "null" ]]; then
    echo "execute_with_gates: notification target = $NOTIFY (live notification dispatch deferred to P4-05)" >&2
  fi
  exit 1
elif [[ "$TERMINAL_REASON" != "completed" ]]; then
  echo "execute_with_gates: irregular_termination classification (terminal_reason=$TERMINAL_REASON)" >&2
  exit 1
fi
exit 0
