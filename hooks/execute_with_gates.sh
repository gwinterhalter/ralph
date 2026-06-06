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
# shellcheck source=../lib/notify.sh
source "$SCRIPT_DIR/../lib/notify.sh"
# shellcheck source=../lib/events.sh
# FUP-0800 Phase 2: event-log emit helper (gate-broker + executor emits, Comprehensive_Event_Log_Spec v1.1).
source "$SCRIPT_DIR/../lib/events.sh"

SEED="${1:?usage: execute_with_gates.sh <seed_path> <iter_dir>}"
ITER_DIR="${2:?usage: execute_with_gates.sh <seed_path> <iter_dir>}"
ITER="$(basename "$ITER_DIR")"
PLAN_PATH="$ITER_DIR/session_plan_${ITER}.md"
RESULT_JSON="$ITER_DIR/execution_result_${ITER}.json"
MCP_CONFIG="$ITER_DIR/mcp_config.json"
STATE_DIR="$(dirname "$(dirname "$ITER_DIR")")"
# FUP-0800 C.0: bind the §4.1 project_id join key + slug (prefer orchestrator-exported values;
# fall back to seed-derived for standalone invocation). Reused at every emit_event call site.
EVENT_PROJECT_ID="${EVENT_PROJECT_ID:-}"
EVENT_SLUG="${EVENT_SLUG:-}"
if [[ -z "$EVENT_SLUG" || "$EVENT_SLUG" == "null" ]]; then
  EVENT_SLUG="$(read_seed_field "$SEED" .initiative.slug 2>/dev/null || echo "")"
fi
if [[ -z "$EVENT_PROJECT_ID" || "$EVENT_PROJECT_ID" == "null" ]]; then
  EVENT_PROJECT_ID="$(read_seed_field "$SEED" .initiative.project_id 2>/dev/null || echo "")"
  [[ -z "$EVENT_PROJECT_ID" || "$EVENT_PROJECT_ID" == "null" ]] && EVENT_PROJECT_ID="$EVENT_SLUG"
fi

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
  # FUP-0807: filter to objects with a `.name` key BEFORE the map() projection. Seed entries
  # that are bare strings (schema-violation per seed.example.md v1.1 per-server {name,command,args,env}
  # shape — observed in Auto_Build_Spec_Closure_Initiative_Seed_v1_5_1.md as `[supabase, filesystem]`)
  # would otherwise hit `jq: error: Cannot index string with string "name"` and abort execute_with_gates
  # with rc=5 → orchestrator HALT EXECUTE_WITH_GATES_UNEXPECTED_EXIT. With the filter, bare-string entries
  # are silently dropped from the merged config (Executor receives `{"mcpServers": {}}` for that pair),
  # matching the pre-Phase-4a-P4-01 fall-through behaviour. A stderr warning enumerates the dropped names
  # so seed authors notice the schema-violation without the orchestrator crashing.
  local dropped
  dropped="$(echo "$seed_block $plan_block" | jq -s -r 'add | map(select(type == "string")) | join(",")')"
  if [[ -n "$dropped" ]]; then
    echo "generate_mcp_config: WARN — dropped bare-name mcp_servers entries (schema-violation, expected {name,command,args,env}): $dropped" >&2
  fi
  # FUP-0841: inject the SUPABASE_ACCESS_TOKEN env reference into any supabase MCP server's env so
  # the generated mcp_config.json SELF-DOCUMENTS the auth dependency. Previously supabase servers
  # carried `env: {}` and authenticated only by IMPLICIT inheritance of SUPABASE_ACCESS_TOKEN from
  # the claude process env — correct when the launching shell exports it, but fragile/undocumented
  # (a run without the export brings the supabase MCP up silently unauthenticated; no schema/validate
  # failure catches it). Uses ${VAR} interpolation (claude expands it from its env at MCP launch) so
  # the secret is NEVER written to the on-disk config. A seed's own explicit supabase env wins (right
  # side of the + merge). Detection = any arg matching `mcp-server-supabase`.
  merged="$(echo "$seed_block $plan_block" | jq -s 'add | map(select(type == "object" and has("name"))) | map({(.name): {command, args, env: (if ((.args // []) | map(select(type=="string") | test("mcp-server-supabase")) | any) then ({"SUPABASE_ACCESS_TOKEN": "${SUPABASE_ACCESS_TOKEN}"} + (.env // {})) else (.env // {}) end)}}) | add // {} | {"mcpServers": .}')"
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

# §10.3 Path A pre-execution gate broker — TWO-PASS structure (§6.3 step 3 / NFR-006 resume wiring).
# Pass 1: classify each request, resolve every gate_dc inline via Answerer, collect gate_human into deferred[].
# Pass 2: handle deferred gate_human — gate_response-precheck (resume entry) before cp + dispatch + pending_gate.
# Rationale: makes BOTH gate classes observable in one run regardless of glob/gate_index order;
# removes the prior "exit at first gate_human" fragility; opens the §6.3 resume path.
deferred_human=()
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
  matched_pc_idx=""  # FUP-0791: track which pre_classification entry matched so we can read its auto_resolve field after the loop
  pc_count="$(read_seed_field "$SEED" '.gate_policy.pre_classification | length')"
  for ((j=0; j<pc_count; j++)); do
    pat="$(read_seed_field "$SEED" ".gate_policy.pre_classification[$j].pattern")"
    if [[ "$pat" == gate_id:* && "$gate_id" == "${pat#gate_id:}" ]]; then
      cls="$(read_seed_field "$SEED" ".gate_policy.pre_classification[$j].class")"
      matched_pc_idx=$j; break
    elif [[ "$pat" == cluster:* ]]; then
      cluster_value="$(jq -r '.cluster // empty' "$req")"
      if [[ "$cluster_value" == "${pat#cluster:}" ]]; then
        cls="$(read_seed_field "$SEED" ".gate_policy.pre_classification[$j].class")"
        matched_pc_idx=$j; break
      fi
    elif [[ "$pat" == contains:* ]]; then
      q_text="$(jq -r '.question_text // empty' "$req")"
      substring="${pat#contains:}"
      if [[ "${q_text,,}" == *"${substring,,}"* ]]; then
        cls="$(read_seed_field "$SEED" ".gate_policy.pre_classification[$j].class")"
        matched_pc_idx=$j; break
      fi
    fi
  done
  # FUP-0791 (seed schema 1.4): auto_resolve handling. When the matched pre_classification entry
  # declares `auto_resolve: <option_id>`, the broker writes a gate_response directly without
  # escalation/Answerer (skips both gate_dc Answerer path and gate_human operator-wait path).
  # Closes the recurring A/A operator pattern (e.g. <skill>-deliverable-scope = A=SKILL.md+tests;
  # <skill>-section-8-4-spec-wiring-coupling = A=defer §8.4 to follow-on spec_bump) — observed
  # 3 times in Phase 6 S2 iter-0003 / 0005 / 0006. Operator override preserved: bump the seed to
  # remove the auto_resolve field if a future skill_build needs a different shape. Backward-compat:
  # entries without auto_resolve OR pre_classification absent → existing classify-and-escalate
  # behaviour unchanged.
  auto_resolve_opt=""
  if [[ -n "$matched_pc_idx" ]]; then
    auto_resolve_opt="$(read_seed_field "$SEED" ".gate_policy.pre_classification[$matched_pc_idx].auto_resolve" 2>/dev/null || echo "")"
    [[ "$auto_resolve_opt" == "null" ]] && auto_resolve_opt=""
  fi
  # FUP-0800 C.7: gate_fire — one per gate_request, upstream of all 3 resolution branches
  # (auto_resolve / gate_human / gate_dc). Capture fire time for the C.8 gate_resolve latency.
  # FUP-0833(a): suppress a duplicate gate_fire on the §6.3 resume leg. On resume,
  # execute_with_gates re-processes the still-present gate_request, but the operator
  # gate_response already exists from the first invocation — a second gate_fire would
  # double-count. Fire only on first encounter (no resolution file yet). auto_resolve /
  # gate_dc write their responses LATER in this same pass, so the file is correctly absent
  # here on a genuine first encounter; only the gate_human resume case finds it present.
  EVENT_GATE_T0="$(date +%s%3N 2>/dev/null || echo 0)"
  _gf_suffix="$(basename "$req" .json)"; _gf_suffix="${_gf_suffix#gate_request_}"
  if [[ ! -f "$ITER_DIR/gate_response_${_gf_suffix}.json" ]]; then
    emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "gate" "gate_fire" "" "$gate_id" "gate" \
      "$(jq -nc --arg gid "$gate_id" --arg cls "$cls" --arg cl "$(jq -r '.cluster // empty' "$req" 2>/dev/null)" '{gate_id:$gid, cls:$cls, cluster:$cl}')"
  fi
  if [[ -n "$auto_resolve_opt" ]]; then
    req_suffix="$(basename "$req" .json)"; req_suffix="${req_suffix#gate_request_}"
    resp_file="$ITER_DIR/gate_response_${req_suffix}.json"
    auto_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    jq -nc --arg gid "$gate_id" --arg opt "$auto_resolve_opt" --arg ts "$auto_ts" --arg idx "$matched_pc_idx" \
       '{gate_id:$gid, selected_option:$opt, reasoning:("broker_auto_resolve per seed.gate_policy.pre_classification[" + $idx + "].auto_resolve = " + $opt), confidence:1.0, classification_check:"auto_resolve_match", source:"broker_auto_resolve", resolved_at:$ts}' \
       > "$resp_file"
    echo "execute_with_gates: broker auto_resolve fired — gate_id=$gate_id matched pc[$matched_pc_idx] resolved to option=$auto_resolve_opt (FUP-0791); gate_response written without Answerer/operator escalation" >&2
    # Audit append to a dedicated log so the operator can re-confirm the auto_resolve decisions post-run.
    mkdir -p "$STATE_DIR/logs"
    jq -nc --arg ts "$auto_ts" --arg it "$ITER" --arg gid "$gate_id" --arg opt "$auto_resolve_opt" --arg idx "$matched_pc_idx" --arg pat "$pat" \
       '{ts:$ts, iter:$it, gate_id:$gid, matched_pattern:$pat, matched_pc_idx:$idx, auto_resolved_option:$opt}' \
       >> "$STATE_DIR/logs/auto_resolve.log"
    # FUP-0800 C.8: gate_resolve (inline, via auto_resolve) — latency from the C.7 fire capture.
    emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "gate" "gate_resolve" \
      "$(( $(date +%s%3N 2>/dev/null || echo 0) - EVENT_GATE_T0 ))" "$gate_id" "gate" \
      "$(jq -nc --arg gid "$gate_id" --arg opt "$auto_resolve_opt" '{gate_id:$gid, mode:"inline", via:"auto_resolve", option:$opt}')"
    continue
  fi
  if [[ "$cls" == "gate_human" ]]; then
    # Defer to pass 2 — but dispatch a classification-time notification iff no operator response exists
    # (avoids re-notifying on resume; only fires on first-time escalation path).
    # FUP-0750: canonical response path is `gate_response_${suffix}.json` (NNNN_MMMM stripped from request basename).
    req_suffix="$(basename "$req" .json)"; req_suffix="${req_suffix#gate_request_}"
    resp_existing="$ITER_DIR/gate_response_${req_suffix}.json"
    if [[ ! -f "$resp_existing" ]]; then
      dispatch_notification "$SEED" "$STATE_DIR" gate_human \
        "$(jq -nc --arg it "$ITER" --arg gid "$gate_id" '{iteration:$it, gate_id:$gid, reason:"broker_classified_gate_human"}')"
    fi
    deferred_human+=("$req")
    continue
  fi
  # gate_dc -> resolve via rl-operator-answerer; answer inlined into plan text by the answerer.
  # FUP-0744: --add-dir + -- required for slash-command resolution from ralph/ CWD.
  # FUP-0750 path-naming normalisation: the Answerer writes its canonical FR-008 JSON
  # to `gate_response_${suffix}.json` (suffix = NNNN_MMMM extracted from the gate_request
  # basename) as a side effect of the skill. The broker captures the slash-command's
  # markdown summary stdout to a SEPARATE filename so it cannot overwrite that canonical
  # JSON. Prior shape (`gate_response_$(basename "$req")` = `gate_response_gate_request_…`)
  # both broke naming + raced/overwrote the Answerer's JSON with markdown text.
  req_suffix="$(basename "$req" .json)"; req_suffix="${req_suffix#gate_request_}"
  resp_file="$ITER_DIR/gate_response_${req_suffix}.json"   # canonical FR-008 path — Answerer-written
  answerer_stdout_file="$ITER_DIR/answerer_stdout_${req_suffix}.md"
  # FUP-0721 (seed schema 1.2): optional answerer_model override; see EXECUTOR_MODEL block
  # below for the read-and-flag idiom. Resolved here (per-dispatch site rather than top-of-
  # script) so the resolution is colocated with the consumer call site for grep-ability.
  ANSWERER_MODEL="$(read_seed_field "$SEED" .answerer_model 2>/dev/null || echo "")"
  [[ "$ANSWERER_MODEL" == "null" ]] && ANSWERER_MODEL=""
  ANSWERER_MODEL_FLAG=""
  [[ -n "$ANSWERER_MODEL" ]] && ANSWERER_MODEL_FLAG="--model $ANSWERER_MODEL"
  # FUP-0842: answerer Role Call segment — closes the v1.2-defined-but-UNWIRED answerer row.
  # role_call before dispatch, role_complete (with the answerer segment duration_ms) after; mirrors
  # the executor role_call/role_complete pattern below. §6.3 best-effort.
  emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "answerer" "role_call" "" "$gate_id" "gate"
  EVENT_ANS_T0="$(date +%s%3N 2>/dev/null || echo 0)"
  # shellcheck disable=SC2086
  claude -p $ANSWERER_MODEL_FLAG --add-dir "$CLAUDE_SKILLS_DIR" -- "/rl-operator-answerer $req" > "$answerer_stdout_file"
  emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "answerer" "role_complete" \
    "$(( $(date +%s%3N 2>/dev/null || echo 0) - EVENT_ANS_T0 ))" "$gate_id" "gate" \
    "$(jq -nc --arg gid "$gate_id" '{gate_id:$gid}')"
  # FUP-NEW (path-mismatch tolerance): rl-operator-answerer occasionally writes the
  # gate_response into an `<iter_dir>/gates/` subdir instead of the canonical `<iter_dir>/`
  # root (stochastic path-confusion when the Answerer notices the state-level `gates/` dir
  # and replicates it inside the iteration tree). Promote any such file to the canonical
  # location before the demotion check so a well-formed Answerer response isn't lost to a
  # spurious gate_human escalation. Long-term fix is in the rl-operator-answerer skill; this
  # is the hook-side safety net.
  if [[ ! -f "$resp_file" && -f "$ITER_DIR/gates/gate_response_${req_suffix}.json" ]]; then
    mv "$ITER_DIR/gates/gate_response_${req_suffix}.json" "$resp_file"
    echo "execute_with_gates: promoted gate_response from $ITER_DIR/gates/ to canonical iter root for $req_suffix" >&2
    # If gates/ is now empty, prune it so it doesn't pollute the iter directory listing.
    rmdir "$ITER_DIR/gates" 2>/dev/null || true
  fi
  # §1.2(b) Answerer-demotion check (FR-009): if the Answerer self-escalated per §5.3
  # (sub-threshold confidence / irreversible / out-of-scope), it emits a gate_escalation
  # artefact alongside / instead of a conformant gate_response. Detect either form
  # (escalation file present, or canonical response missing the 4-field FR-008 payload).
  demote_artefact="$(find "$ITER_DIR" -maxdepth 1 -name "gate_escalation_${req_suffix}*.md" 2>/dev/null | head -1)"
  demoted=0
  if [[ -n "$demote_artefact" ]]; then
    demoted=1
  elif [[ ! -f "$resp_file" ]]; then
    demoted=1
  elif ! jq -e '((.selected_option // null) != null or (.custom_text // null) != null) and ((.reasoning // "") | length > 0) and ((.confidence // null) | type == "number")' "$resp_file" >/dev/null 2>&1; then
    demoted=1
  fi
  if (( demoted == 1 )); then
    echo "execute_with_gates: Answerer demoted gate_dc $gate_id to gate_human" >&2
    dispatch_notification "$SEED" "$STATE_DIR" gate_human \
      "$(jq -nc --arg it "$ITER" --arg gid "$gate_id" '{iteration:$it, gate_id:$gid, reason:"answerer_demote"}')"
    deferred_human+=("$req")
  else
    # FUP-0800 C.8: gate_resolve (inline, via answerer) — gate_dc resolved without demotion.
    emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "gate" "gate_resolve" \
      "$(( $(date +%s%3N 2>/dev/null || echo 0) - EVENT_GATE_T0 ))" "$gate_id" "gate" \
      "$(jq -nc --arg gid "$gate_id" '{gate_id:$gid, mode:"inline", via:"answerer"}')"
  fi
done
shopt -u nullglob

# Pass 2: handle deferred gate_human — §2.2 gate_response-before-escalate + §2.3 pending_gate.
# FUP-0750: response precheck reads canonical `gate_response_${suffix}.json` (matching the
# Answerer's side-effect write path + the orchestrator §6.3 resume scan pattern).
any_blocked=0
for req in "${deferred_human[@]}"; do
  basename_req="$(basename "$req")"
  req_suffix="$(basename "$req" .json)"; req_suffix="${req_suffix#gate_request_}"
  resp_file="$ITER_DIR/gate_response_${req_suffix}.json"
  # FUP-0852: a deferred gate_human is RESOLVED only if a WELL-FORMED, FR-008-complete gate_response
  # exists AND the Answerer did not self-escalate (no gate_escalation_${suffix}*.md). Previously this
  # gated on FILE EXISTENCE alone — so a malformed/invalid Answerer response (the very condition that
  # triggered the pass-1 demotion) was re-admitted as "resolved" and the gate was silently BYPASSED
  # instead of blocking for the operator. Mirror the pass-1 validity check + require no escalation artefact.
  resp_resolved=0
  if [[ -f "$resp_file" ]] \
     && jq -e '((.selected_option // null) != null or (.custom_text // null) != null) and ((.reasoning // "") | length > 0) and ((.confidence // null) | type == "number")' "$resp_file" >/dev/null 2>&1 \
     && ! compgen -G "$ITER_DIR/gate_escalation_${req_suffix}*.md" >/dev/null 2>&1; then
    resp_resolved=1
  elif [[ -f "$resp_file" ]]; then
    echo "execute_with_gates: gate_human ${req_suffix} gate_response present but INVALID/incomplete or accompanied by a self-escalation — NOT treating as resolved; escalating to operator (FUP-0852)" >&2
  fi
  if [[ "$resp_resolved" -eq 1 ]]; then
    # Valid answer (async operator response, or a valid prior resolution) — inline (the answer is
    # consumed by the next role call's plan reading) and proceed, no re-escalation.
    echo "execute_with_gates: gate_human ${req_suffix} resolved by valid gate_response — inlined, proceeding" >&2
    # FUP-0800 C.8: gate_resolve (operator) — resolved via async operator gate_response.
    emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "gate" "gate_resolve" "" \
      "$(jq -r '.gate_id // empty' "$req" 2>/dev/null)" "gate" \
      "$(jq -nc --arg gid "$(jq -r '.gate_id // empty' "$req" 2>/dev/null)" '{gate_id:$gid, mode:"operator"}')"
    continue
  fi
  # Response absent — escalate: cp to escalations/, write pending_gate, mark blocked.
  mkdir -p "$STATE_DIR/escalations"
  cp "$req" "$STATE_DIR/escalations/$basename_req"
  gate_id="$(jq -r '.gate_id // empty' "$req")"
  echo "execute_with_gates: gate_human escalation for $gate_id" >&2
  # FUP-0842: gate_escalate — a gate_human escalated to the operator (awaiting async response);
  # distinct from gate_fire, pairs with gate_resolve(mode=operator) for §13 Q7 operator-response
  # latency. subject_id=gate_id + subject_kind="gate" per the spec §6.2 envelope. §6.3 best-effort.
  emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "gate" "gate_escalate" "" "$gate_id" "gate" \
    "$(jq -nc --arg gid "$gate_id" --arg it "$ITER" '{gate_id:$gid, awaiting:"operator", iteration:$it}')"
  # §2.3: write pending_gate to state_snapshot.json (the value §6.3 step 3 consumes).
  mkdir -p "$STATE_DIR"
  if [[ -f "$STATE_DIR/state_snapshot.json" ]]; then
    jq --arg it "$ITER" --arg gi "$basename_req" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
       '.pending_gate = {iteration:$it, gate_request:$gi, written_at:$ts}' \
       "$STATE_DIR/state_snapshot.json" > "$STATE_DIR/state_snapshot.json.tmp" \
       && mv "$STATE_DIR/state_snapshot.json.tmp" "$STATE_DIR/state_snapshot.json"
  else
    jq -n --arg it "$ITER" --arg gi "$basename_req" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
       '{pending_gate:{iteration:$it, gate_request:$gi, written_at:$ts}}' > "$STATE_DIR/state_snapshot.json"
  fi
  any_blocked=1
done
if (( any_blocked == 1 )); then
  exit 1
fi

# Execute (§5.2 canonical invocation).
PERMISSION_POSTURE="$(read_seed_field "$SEED" .permission_posture)"
PER_CALL_CAP="$(read_seed_field "$SEED" '.budget.per_call_usd_cap // .budget.tokens_usd')"
MAX_TURNS="$(read_seed_field "$SEED" '.budget.max_turns_per_call // 200')"
# FUP-0721 (seed schema 1.2): optional executor_model override. read_seed_field returns the
# literal string "null" when the field is absent (yq behaviour). Treat "null" + empty as
# "no override; omit --model and inherit CLI default". When set to a real model id (e.g.
# "claude-sonnet-4-6"), the orchestrator passes --model <value> to claude --print so the
# Plan-side model binding (e.g. Plan SA Q3 "Sonnet 4.6") is enforceable rather than
# aspirational. Backward-compat: existing schema-1.1 seeds resolve to no-op (CLI default).
EXECUTOR_MODEL="$(read_seed_field "$SEED" .executor_model 2>/dev/null || echo "")"
[[ "$EXECUTOR_MODEL" == "null" ]] && EXECUTOR_MODEL=""
EXECUTOR_MODEL_FLAG=""
[[ -n "$EXECUTOR_MODEL" ]] && EXECUTOR_MODEL_FLAG="--model $EXECUTOR_MODEL"
# OLB-08 C2 (operator gate_response_0015_0000 = option A): optional NARROW allow-rule for the
# checkpoint live-spawn. The seed may declare executor_allowed_tools as a YAML list of Claude Code
# permission patterns (e.g. "Bash(OLB_C2_LIVE=1 python -m pytest tests/test_c2_single_project_e2e.py:*)").
# When present they are passed as --allowedTools so the integration_checkpoint Executor's
# operator-sanctioned orchestrator.sh spawn is pre-approved under --permission-mode auto (per the
# permission decision-order: an allow-rule match resolves immediately, ahead of the auto-mode
# classifier). Narrow by construction — ONLY the declared patterns are pre-approved; the posture is
# otherwise unchanged, and absent the field this is a no-op (every pre-OLB-08 seed unaffected).
EXEC_ALLOW=()
_eat_valid=()
_eat_fm="$(awk '/^---[[:space:]]*$/{c++; next} c==1{print} c==2{exit}' "$SEED")"
while IFS= read -r _eat_p; do [[ -n "$_eat_p" && "$_eat_p" != "null" ]] && _eat_valid+=("$_eat_p"); done \
  < <(printf '%s\n' "$_eat_fm" | yq -r '.executor_allowed_tools[]' 2>/dev/null || true)
if (( ${#_eat_valid[@]} > 0 )); then
  EXEC_ALLOW=(--allowedTools "${_eat_valid[@]}")
  echo "execute_with_gates: executor_allowed_tools active — ${#_eat_valid[@]} narrow Bash pattern(s) pre-approved via --allowedTools (OLB-08 C2 spawn consent, gate_response_0015_0000=A)" >&2
fi
# FUP-0752: seed convention is to declare permission_posture as the FULL flag string
# (e.g. "--permission-mode auto"), but `claude --permission-mode <value>` wants just the
# VALUE. Strip the leading `--permission-mode ` prefix when present (liberal accept of
# either form). Without this, claude rejects `--permission-mode "--permission-mode auto"`
# with "Allowed choices are acceptEdits, auto, bypassPermissions, default, dontAsk, plan."
posture_value="${PERMISSION_POSTURE#--permission-mode }"
# OLB-08+ (operator decision 2026-06-05): per-shape permission posture for integration_checkpoint.
# A C2/C3/C4 checkpoint's live drain legitimately spawns a real orchestrator.sh -> nested `claude -p`
# AND does post-completion housekeeping (e.g. `rm -rf` of the nested oltest state). Under
# --permission-mode auto the classifier denies these and a narrow --allowedTools rule is brittle
# (each command variant re-denies — e.g. the OLB-08 close was blocked only by a post-completion `rm`
# cleanup denial after the deliverable had already succeeded). For the bounded, oltest-capped
# integration_checkpoint iterations ONLY, use the seed's checkpoint_permission_posture
# (bypassPermissions): the classifier is off for that one iteration, but the orchestrator's
# read-only-paths scan + budget cap + §10.5 post-exec gate remain enforced. Non-checkpoint
# iterations keep the seed's default permission_posture (auto). No-op when the seed omits the field.
# Shape extraction (FUP-0853): real Planner plans format the shape as a markdown bullet
# `- **shape:** integration_checkpoint` (NOT a YAML-frontmatter `^shape:` line — the prior awk
# matched only the latter and was a silent no-op against every real plan, so the checkpoint
# override never engaged). Match either the bullet/bold form OR a bare `shape:` line, take the
# FIRST hit, and anchor on the literal `shape:` field so decoy lines ("- **shape verification
# binding:**", "seam-shape probe", "no table re-shape") never match.
_plan_shape="$(sed -nE 's/^[[:space:]]*(-[[:space:]]+)?\*{0,2}shape:\*{0,2}[[:space:]]*([A-Za-z_]+).*/\2/p' "$PLAN_PATH" 2>/dev/null | head -1)"
CKPT_POSTURE="$(read_seed_field "$SEED" .checkpoint_permission_posture 2>/dev/null || echo "")"
[[ "$CKPT_POSTURE" == "null" ]] && CKPT_POSTURE=""
if [[ "$_plan_shape" == "integration_checkpoint" && -n "$CKPT_POSTURE" ]]; then
  posture_value="${CKPT_POSTURE#--permission-mode }"
  echo "execute_with_gates: integration_checkpoint shape — overriding permission posture to '$posture_value' per seed.checkpoint_permission_posture (OLB-08+ checkpoint spawn+housekeeping consent; read-only-scan + budget + §10.5 gate still enforced)" >&2
fi
# FUP-0764: capture the CLI's authoritative JSON envelope to a collision-proof dotfile temp,
# then atomically mv it into place. The executed agent can itself write a file named
# execution_result_${ITER}.json as a side effect (observed on the §6.3 resume leg: an Executor
# hand-authored the envelope "to leave the exit-0 shape"), which races the `>` redirect's shared
# fd and leaves a concatenated two-document file (jsonschema "Extra data: line 2"). Redirecting
# to a name the agent does not target + mv -f guarantees the CLI envelope is the sole final
# content — same orchestrator-owns-its-output-files invariant the FUP-0756 fix applied to the
# Consumer-written state files.
RESULT_JSON_TMP="$ITER_DIR/.execution_result_${ITER}.cli.json"
# FUP-0800 C.9: executor role_call + role_complete (duration across the claude --print call).
emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "executor" "role_call"
EVENT_EX_T0="$(date +%s%3N 2>/dev/null || echo 0)"
# shellcheck disable=SC2086
claude --print --output-format json \
       --permission-mode "$posture_value" \
       $EXECUTOR_MODEL_FLAG \
       "${EXEC_ALLOW[@]}" \
       --strict-mcp-config --mcp-config "$MCP_CONFIG" \
       --max-budget-usd "$PER_CALL_CAP" --max-turns "$MAX_TURNS" \
       < "$PLAN_PATH" > "$RESULT_JSON_TMP"
mv -f "$RESULT_JSON_TMP" "$RESULT_JSON"
emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "executor" "role_complete" \
  "$(( $(date +%s%3N 2>/dev/null || echo 0) - EVENT_EX_T0 ))" "" "" \
  "$(jq -nc --arg tr "$(jq -r '.terminal_reason // empty' "$RESULT_JSON" 2>/dev/null)" '{terminal_reason:$tr}')"
# FUP-0842: per-call cost/latency primitive (llm_call) for the executor claude --print call —
# tokens/cost from the CLI JSON envelope; duration_ms = the executor call wall-clock (EVENT_EX_T0);
# subject_kind="llm_call" per the spec §6.2 envelope addition. §6.3 best-effort.
emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "executor" "llm_call" \
  "$(( $(date +%s%3N 2>/dev/null || echo 0) - EVENT_EX_T0 ))" "" "llm_call" \
  "$(jq -nc \
     --arg m "$(jq -r '.modelUsage // {} | keys[0] // (.model // "")' "$RESULT_JSON" 2>/dev/null || echo "")" \
     --argjson i "$(jq -r '.usage.input_tokens // 0' "$RESULT_JSON" 2>/dev/null || echo 0)" \
     --argjson o "$(jq -r '.usage.output_tokens // 0' "$RESULT_JSON" 2>/dev/null || echo 0)" \
     --argjson c "$(jq -r '.usage.cache_read_input_tokens // 0' "$RESULT_JSON" 2>/dev/null || echo 0)" \
     --argjson cost "$(jq -r '.total_cost_usd // 0' "$RESULT_JSON" 2>/dev/null || echo 0)" \
     '{role:"executor", model:$m, input_tokens:$i, output_tokens:$o, cache_read_tokens:$c, cost_usd:$cost}')"

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
# FUP-0765 / FUP-0779 / FUP-0804 horn (C): classify each permission_denial as either
# `verification_spawn` (nested `claude -p` / `claude --print` Bash invocation — the Executor
# spawning an audit / verification subprocess; benign, since the verification skill is the
# legitimate recipient of the permission boundary) or `deliverable_blocking` (any other
# denial — actual permission overreach that would corrupt the run).
#
# Exit 1 ONLY when at least one denial is deliverable_blocking. When ALL denials are
# verification_spawn, log + audit but continue, so the iteration's Consumer can still run
# against the Executor's completed deliverables. This eliminates the FUP-0765 failure mode
# (Executor completes all deliverables but headless auto-mode denies its nested
# `claude -p --dangerously-skip-permissions` for `run cf-doc-reviewer` → permission_denials
# non-empty → exit 1 → Consumer never runs → consumer.json single-object validation never
# happens) without regressing the Executor's `--permission-mode auto` security posture.
#
# Pairs with the Path A interim (rl-iteration-consumer v1_4 empty-binding rule +
# rl-initiative-planner v1.3 inline-audit verification_policy branch, both promoted+pushed
# 2026-05-28 commit 082a9ea); Path A reduces the SURFACE of verification_spawn denials by
# moving inline-audit out of the Consumer's path, but the Executor itself may still spawn
# nested claude for verification per session_plan §5 instructions — this horn handles those
# remaining cases at the gate rather than upstream at the planner.
#
# Detection regex: tool_name == "Bash" AND tool_input.command matches
# `\bclaude\s+(-p|--print)\b` (the two CLI invocation forms used by all cf-*/rl-* skills).
# Conservative by design — only denials matching BOTH conditions are reclassified; any other
# denial (incl. non-Bash, non-claude Bash commands, MCP tool denials) remains
# deliverable_blocking.
TERMINAL_REASON="$(jq -r '.terminal_reason' "$RESULT_JSON")"
DENIALS_COUNT="$(jq -r '.permission_denials | length' "$RESULT_JSON")"
if [[ "$DENIALS_COUNT" -ne 0 ]]; then
  # Count verification_spawn-classified denials. The jq expression uses .tool_name first
  # (current claude --output-format json shape per FUP-0737), falling back to .tool (legacy
  # synthetic-fixture shape). The command regex matches both `claude -p` and `claude --print`
  # invocation forms (word-boundary guarded to avoid matching e.g. `claudette --print-only`).
  VERIFICATION_SPAWN_COUNT="$(jq -r '[.permission_denials[] | select(((.tool_name // .tool // "") == "Bash") and (((.tool_input.command // "") | test("\\bclaude\\s+(-p|--print)\\b")) or ((.reason // "") | test("\\bclaude\\s+(-p|--print)\\b"))))] | length' "$RESULT_JSON")"
  DELIVERABLE_BLOCKING_COUNT=$((DENIALS_COUNT - VERIFICATION_SPAWN_COUNT))

  if [[ "$DELIVERABLE_BLOCKING_COUNT" -gt 0 ]]; then
    # At least one deliverable_blocking denial — existing exit-1 path preserved.
    mkdir -p "$STATE_DIR/escalations"
    esc_file="$STATE_DIR/escalations/auto_mode_denial_${ITER}_$(date -u +%s).json"
    jq --arg iter "$ITER" --arg vsc "$VERIFICATION_SPAWN_COUNT" --arg dbc "$DELIVERABLE_BLOCKING_COUNT" \
       '{iteration: $iter, classification: "auto_mode_denial", denials_count: (.permission_denials | length), verification_spawn_count: ($vsc | tonumber), deliverable_blocking_count: ($dbc | tonumber), permission_denials: .permission_denials, terminal_reason: .terminal_reason}' "$RESULT_JSON" > "$esc_file"
    echo "execute_with_gates: auto_mode_denial classification — escalation $esc_file (denials=$DENIALS_COUNT, verification_spawn=$VERIFICATION_SPAWN_COUNT, deliverable_blocking=$DELIVERABLE_BLOCKING_COUNT)" >&2
    NOTIFY="$(read_seed_field "$SEED" '.notification_channel' 2>/dev/null || echo "")"
    if [[ -n "$NOTIFY" && "$NOTIFY" != "null" ]]; then
      dispatch_notification "$SEED" "$STATE_DIR" iteration_failed \
        "$(jq -nc --arg it "$ITER" --arg dc "$DENIALS_COUNT" --arg dbc "$DELIVERABLE_BLOCKING_COUNT" --arg vsc "$VERIFICATION_SPAWN_COUNT" \
          '{iteration:$it, reason:"auto_mode_denial_deliverable_blocking", denials_count:($dc|tonumber), deliverable_blocking_count:($dbc|tonumber), verification_spawn_count:($vsc|tonumber)}')"
    fi
    exit 1
  else
    # ALL denials are verification_spawn — log + audit + continue. The Executor completed its
    # deliverables; the denied nested claude -p was an optional verification spawn that didn't
    # block the actual work product. The Consumer will run against the completed deliverables
    # in the next phase. Pairs with FUP-0809 + lib/notify.sh resilient observability.
    mkdir -p "$STATE_DIR/logs"
    audit_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    jq --arg ts "$audit_ts" --arg iter "$ITER" --arg dc "$DENIALS_COUNT" \
       '{ts:$ts, iter:$iter, classification:"verification_spawn_all", denials_count:($dc|tonumber), permission_denials:.permission_denials}' \
       "$RESULT_JSON" >> "$STATE_DIR/logs/verification_spawn_denials.log"
    echo "execute_with_gates: all $DENIALS_COUNT permission_denials classified verification_spawn (FUP-0765/0779/0804 horn C) — proceeding without exit (audit: $STATE_DIR/logs/verification_spawn_denials.log)" >&2
  fi
fi
if [[ "$TERMINAL_REASON" != "completed" ]]; then
  echo "execute_with_gates: irregular_termination classification (terminal_reason=$TERMINAL_REASON)" >&2
  exit 1
fi

# FUP-0854: report-emission recovery. The Executor intermittently finishes a build/checkpoint
# (commit+push done, terminal_reason=completed, no deliverable-blocking denials) WITHOUT writing
# execution_report_${ITER}.md (with its '## Items closed' section) — the Consumer then HALTs
# `failed_report_missing` (observed iter-0020, iter-0026) and cannot auto-close even though the
# deliverable is genuinely complete + committed + green. When the report is absent on a
# clean-completed build/checkpoint shape, do ONE bounded `--resume` follow-up that re-enters the
# SAME Executor session and asks it to write the report from the work it just finished. The report
# stays genuinely Executor-authored (NOT a JSON->report synthesis), so the Consumer's
# no-infer-from-result discipline (Core Principle 6) AND its independent substrate re-verification
# both remain in force — this only restores the missing deliverable, it cannot manufacture a close.
# Best-effort and fully guarded: never alters this hook's exit code, fires at most once, requests
# NO code/git change, and is bounded by --max-budget-usd. No-op for noop shapes, when the report
# is already complete, or when no Executor session_id is available.
#
# T1#2 (deliverable-completeness self-check): the recovery now fires not only when the report
# FILE is absent, but also when it exists yet lacks the canonical '## Items closed' section the
# Consumer parses (Failure Protocol Row 5) — a report with no section is as un-closeable as no
# report. `_report_complete` = the file exists AND carries that heading; the recovery prompt
# already mandates the section, so it covers both the missing-file and missing-section cases.
case "$_plan_shape" in
  component_build|integration_checkpoint|skill_build)
    _report_path="$ITER_DIR/execution_report_${ITER}.md"
    _report_complete() { [[ -f "$_report_path" ]] && grep -qiE '^##[[:space:]]+Items[[:space:]]+closed' "$_report_path"; }
    if ! _report_complete; then
      _ex_session="$(jq -r '.session_id // empty' "$RESULT_JSON" 2>/dev/null || echo "")"
      mkdir -p "$STATE_DIR/logs"
      if [[ -n "$_ex_session" ]]; then
        echo "execute_with_gates: FUP-0854/T1#2 — terminal_reason=completed but $_report_path is absent or lacks a '## Items closed' section; one bounded --resume follow-up to have the Executor emit/complete its report (no code/git change requested)" >&2
        _recovery_prompt="The build and all git steps for iteration ${ITER} are ALREADY complete, committed, tagged, and pushed — do NOT modify, rebuild, re-commit, re-tag, or push anything, and do NOT run any verification again. The only thing missing is the session plan's terminal deliverable: a COMPLETE report file at the absolute path ${_report_path}. Write (or complete) that file NOW from the work you just finished in this same session, using the Write tool. It MUST contain a top-level '## Items closed' section listing each registry work-item you closed this iteration as a bullet in the form '- OLB-NN — <title>' with its closing evidence (commit SHA, tag, cf-code-review BLOCKER/DRIFT counts, cf-pytest passed/skipped counts). If you closed no item this iteration, write the '## Items closed' heading followed by the single word 'none'. Then output only a one-line confirmation. Make no other changes."
        # shellcheck disable=SC2086
        claude --print --output-format json --resume "$_ex_session" \
               --permission-mode "$posture_value" \
               $EXECUTOR_MODEL_FLAG \
               "${EXEC_ALLOW[@]}" \
               --strict-mcp-config --mcp-config "$MCP_CONFIG" \
               --max-budget-usd "$PER_CALL_CAP" --max-turns 12 \
               <<<"$_recovery_prompt" >> "$STATE_DIR/logs/report_recovery.log" 2>&1 || true
        if _report_complete; then
          echo "execute_with_gates: FUP-0854/T1#2 — recovery SUCCEEDED, $_report_path now present with a '## Items closed' section" >&2
        else
          echo "execute_with_gates: FUP-0854/T1#2 — recovery did NOT produce a complete report; Consumer will classify failed_report_missing as before (no regression)" >&2
        fi
      else
        echo "execute_with_gates: FUP-0854/T1#2 — $_report_path absent/incomplete and no session_id in result; skipping recovery (Consumer will HALT failed_report_missing)" >&2
      fi
    fi
    ;;
esac
exit 0
