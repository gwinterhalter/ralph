#!/usr/bin/env bash
# orchestrator.sh — Ralph-loop controller (Initiative_Orchestrator_Spec §13.1 verbatim shape).
# Adaptations (plan Step 14): source lib/seed.sh; resolve STATE_DIR to an absolute path
#   (WORKSPACE_ROOT/state_dir_relative); §6.3 resumability startup; §6.1 mkdir scaffolding;
#   $SCRIPT_DIR-relative hook paths; orchestrator.log appends.
set -euo pipefail

# FUP-0740: disable MSYS path-conversion so slash-prefixed prompt args and K:/ paths
# survive on Git Bash for Windows (else "/rl-initiative-planner …" → C:/Program Files/Git/…
# and K:-drive paths get colon-split). Confirmed root cause: execution report §3 Run 1 + Diagnostic 1.
# FUP-0823: localized via env-prefix on the `claude -p` invocation in run_claude_json
# (see L88 area), NOT exported globally — global export breaks WinGet jq.exe (Windows-native
# binary) on /tmp/... paths. Localized form preserves FUP-0740 fix scope (claude -p only)
# without leaking the path-conversion-disable to every other binary (jq, mv, etc.).
# FUP-0739: skills-tree root (the dir CONTAINING .claude/skills) — passed via --add-dir so
# `claude -p` resolves the rl-* slash commands from the ralph/ CWD (skills live in a SIBLING
# tree, not an ancestor of ralph/). Env-overridable; portable across drive/path changes (Q1 default).
# EXPORT (not bare `:=`) so child hooks under `set -euo pipefail` (which makes unset vars an error)
# inherit the value — without export the variable lives only in orchestrator.sh's shell.
export CLAUDE_SKILLS_DIR="${CLAUDE_SKILLS_DIR:-K:/Claude Code Factory/V3/Project_Docs}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/seed.sh
source "$SCRIPT_DIR/lib/seed.sh"
# shellcheck source=lib/notify.sh
source "$SCRIPT_DIR/lib/notify.sh"
# shellcheck source=lib/heartbeat.sh
# FUP-0798: per-iteration workstreams-row UPSERT for operator-awareness during long-running
# headless RL runs. No-op when seed.heartbeat.workstream_id is absent or env vars unset.
source "$SCRIPT_DIR/lib/heartbeat.sh"
# shellcheck source=lib/command_dispatch.sh
# FUP-0815: operator-command channel — state_dir/commands/ watch dir, polled at iter boundaries.
# Dispatches pause / bump_budget / query_register_state via case in command_dispatch_run.
source "$SCRIPT_DIR/lib/command_dispatch.sh"
# shellcheck source=lib/events.sh
# FUP-0800 Phase 2: local-first NDJSON event log + idempotent Supabase sync (Comprehensive_Event_Log_Spec v1.1).
source "$SCRIPT_DIR/lib/events.sh"

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
# FUP-0831: planner/consumer dispatch (run_claude_json) must run headless `claude -p` under the
# seed's permission posture — same as the Executor in execute_with_gates.sh (§--permission-mode
# "$posture_value"). Without it the role call runs in DEFAULT mode, which in non-interactive `-p`
# DENIES the Planner's session_plan_NNNN.md write (and the Consumer's state writes) → every
# iteration fails at the missing plan file → MAX_ITERATIONS cap HALT. Previously masked on
# provisioned hosts by a gitignored .claude/settings.local.json grant; surfaces on fresh clones.
# Strip the leading "--permission-mode " prefix per FUP-0752 (seeds declare the full flag string);
# default to "auto" when the field is absent/null (matches the Executor's effective default).
POSTURE_VALUE="$(read_seed_field "$SEED" .permission_posture 2>/dev/null || echo "")"
POSTURE_VALUE="${POSTURE_VALUE#--permission-mode }"
[[ -z "$POSTURE_VALUE" || "$POSTURE_VALUE" == "null" ]] && POSTURE_VALUE="auto"

# FUP-0800 Phase 2 (event log): resolve the §4.1 project_id join key + initiative slug once for
# reuse at every emit_event call site. project_id := seed .initiative.project_id when present, else
# the initiative slug (matches projects.project_id for single-project initiatives), else the
# WORKSPACE_ROOT basename. Slug := .initiative.slug.
EVENT_SLUG="$(read_seed_field "$SEED" .initiative.slug 2>/dev/null || echo "")"
[[ -z "$EVENT_SLUG" || "$EVENT_SLUG" == "null" ]] && EVENT_SLUG="$(basename "$WORKSPACE_ROOT")"
EVENT_PROJECT_ID="$(read_seed_field "$SEED" .initiative.project_id 2>/dev/null || echo "")"
[[ -z "$EVENT_PROJECT_ID" || "$EVENT_PROJECT_ID" == "null" ]] && EVENT_PROJECT_ID="$EVENT_SLUG"
export EVENT_PROJECT_ID EVENT_SLUG

log() { mkdir -p "$STATE_DIR/logs"; printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$STATE_DIR/logs/orchestrator.log"; }

# run_claude_json — wraps `claude -p` with --output-format json + --max-budget-usd;
# captures total_cost_usd into the running spend; HALTs orchestrator if cumulative
# spend exceeds cap. FUP-0720.
# FUP-0721 (seed schema 1.2): optional model override via env var ROLE_MODEL. Caller
# resolves the seed field per role (`planner_model` / `consumer_model`) immediately
# before calling; ROLE_MODEL="" → CLI default (no --model flag passed). Backward-compat
# preserved — pre-1.2 seeds have no model fields, so ROLE_MODEL is always empty.
# Usage: ROLE_MODEL="$(read_seed_field "$SEED" .planner_model)" run_claude_json <out> <prompt>
run_claude_json() {
  local out_file="$1"; shift
  local current_spend remaining_budget call_cost new_total role_model_flag=""
  # FUP-0721: convert empty/"null"/unset ROLE_MODEL → no flag; concrete value → `--model <val>`.
  local rm_val="${ROLE_MODEL:-}"
  [[ "$rm_val" == "null" ]] && rm_val=""
  [[ -n "$rm_val" ]] && role_model_flag="--model $rm_val"
  [[ -f "$RUNNING_SPEND_FILE" ]] || echo '{"total_spend_usd": 0.0}' > "$RUNNING_SPEND_FILE"
  current_spend="$(jq -r '.total_spend_usd' "$RUNNING_SPEND_FILE")"
  # FUP-0815: effective_cap = max($BUDGET_CAP, $budget_override) — operator bumps via
  # \btw bump <usd> write $STATE_DIR/budget_override.json; never reduce below seed cap.
  local effective_cap="$BUDGET_CAP"
  if [[ -f "$STATE_DIR/budget_override.json" ]]; then
    effective_cap="$(jq -rn --argjson seed "$BUDGET_CAP" --argjson ovr "$(jq -r '.budget_cap_usd' "$STATE_DIR/budget_override.json")" '[$seed, $ovr] | max')"
  fi
  remaining_budget="$(jq -rn --argjson cap "$effective_cap" --argjson cur "$current_spend" '$cap - $cur')"
  if awk "BEGIN { exit !($remaining_budget <= 0) }"; then
    log "HALT: BUDGET_EXHAUSTED before next claude -p (spend=$current_spend cap=$BUDGET_CAP)"
    dispatch_notification "$SEED" "$STATE_DIR" budget_exhausted "$(jq -nc --arg sp "$current_spend" --arg cap "$BUDGET_CAP" '{iteration:"", reason:"run_claude_json_pre_call", spend:$sp, cap:$cap}')"
    echo "HALT: BUDGET_EXHAUSTED" >&2; exit 2
  fi
  # FUP-0823: env-prefix MSYS_NO_PATHCONV=1 + MSYS2_ARG_CONV_EXCL='*' ONLY on the claude
  # invocation (was previously exported globally at L11; broke WinGet jq.exe on /tmp/...
  # paths everywhere). Localized form preserves FUP-0740 slash-prefix preservation scope
  # without leaking the path-conversion-disable to jq / mv / etc.
  # shellcheck disable=SC2086
  MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
    claude -p $role_model_flag --permission-mode "$POSTURE_VALUE" --output-format json --max-budget-usd "$remaining_budget" --add-dir "$CLAUDE_SKILLS_DIR" -- "$@" > "$out_file"
  call_cost="$(jq -r '.total_cost_usd // 0' "$out_file")"
  new_total="$(jq -rn --argjson cur "$current_spend" --argjson cc "$call_cost" '$cur + $cc')"
  jq --argjson nt "$new_total" '.total_spend_usd = $nt' "$RUNNING_SPEND_FILE" > "$RUNNING_SPEND_FILE.tmp" \
    && mv "$RUNNING_SPEND_FILE.tmp" "$RUNNING_SPEND_FILE"
  log "claude -p call_cost=$call_cost running_total=$new_total cap=$BUDGET_CAP model=${rm_val:-<cli-default>}"
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
# FUP-0769: hash via stdin so the filename never appears in sha256sum output —
# a Windows backslash path would otherwise make coreutils prepend '\' to the
# digest line, corrupting the §6.3 resume hash comparison on a false mismatch.
registry_hash() { [[ -f "$1" ]] && sha256sum < "$1" | cut -d' ' -f1 || echo "MISSING"; }

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
    pending_iter="$(jq -r '.pending_gate.iteration // empty' "$STATE_DIR/state_snapshot.json")"
    if [[ -n "$pending_iter" ]]; then
      pending_iter_dir="$STATE_DIR/iterations/$pending_iter"
      # §2.4 controller resume — present matching gate_response → re-invoke broker for the pending iteration.
      response_count=$(find "$pending_iter_dir" -maxdepth 1 -name "gate_response_${pending_iter}_*.json" 2>/dev/null | wc -l)
      if (( response_count > 0 )); then
        log "RESUME: operator gate_response found for iteration $pending_iter — re-running execute_with_gates"
        set +e
        "$SCRIPT_DIR/hooks/execute_with_gates.sh" "$SEED" "$pending_iter_dir"
        resume_rc=$?
        set -e
        case $resume_rc in
          0)
            log "RESUME: broker resolved + Executor ran — clearing pending_gate, running Consumer for $pending_iter"
            jq 'del(.pending_gate)' "$STATE_DIR/state_snapshot.json" > "$STATE_DIR/state_snapshot.json.tmp" \
              && mv "$STATE_DIR/state_snapshot.json.tmp" "$STATE_DIR/state_snapshot.json"
            emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$pending_iter" "consumer" "role_call"
            EVENT_CN_T0="$(date +%s%3N 2>/dev/null || echo 0)"
            ROLE_MODEL="$(read_seed_field "$SEED" .consumer_model 2>/dev/null || echo "")" \
              run_claude_json "$pending_iter_dir/consumer.json" "/rl-iteration-consumer $STATE_DIR $pending_iter_dir"
            emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$pending_iter" "consumer" "phase_complete" \
              "$(( $(date +%s%3N 2>/dev/null || echo 0) - EVENT_CN_T0 ))" "" "" \
              "$(jq -nc --argjson cs "$(jq -r '.total_spend_usd // 0' "$RUNNING_SPEND_FILE" 2>/dev/null || echo 0)" '{cumulative_spend:$cs}')"
            # FUP-0833(b): emit iteration_end for the resumed iteration — the main-loop emit
            # (C.5, L422) is bypassed on the §6.3 resume path, so without this the resumed
            # iteration carries phase_complete but no iteration_end. duration_ms = wall-clock
            # from the iteration_start marker (.iter_start mtime; spans the operator gate-wait,
            # which is the true iteration span). Empty duration if the marker is absent.
            _rs_t0="$(stat -c %Y "$pending_iter_dir/.iter_start" 2>/dev/null || echo 0)"
            emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$pending_iter" "orchestrator" "iteration_end" \
              "$([[ "$_rs_t0" != "0" ]] && echo "$(( $(date +%s%3N 2>/dev/null || echo 0) - _rs_t0 * 1000 ))" || echo "")" \
              "" "" "$(jq -nc '{resumed:true}')"
            ;;
          1)
            log "RESUME: broker still pending after re-run — re-block"
            dispatch_notification "$SEED" "$STATE_DIR" gate_human "$(jq -nc --arg it "$pending_iter" '{iteration:$it, reason:"resume_still_pending"}')"
            echo "BLOCKED: pending gate_human (iteration $pending_iter)" >&2
            exit 0
            ;;
          2)
            log "HALT: execute_with_gates exit 2 on resume (read-only violation)"
            echo "HALT: READ_ONLY_BOUNDARY_VIOLATION (iteration $pending_iter)" >&2
            exit 3
            ;;
          *)
            log "HALT: execute_with_gates returned unexpected exit $resume_rc on resume"
            echo "HALT: EXECUTE_WITH_GATES_UNEXPECTED_EXIT (rc=$resume_rc, resume iteration $pending_iter)" >&2
            exit 3
            ;;
        esac
      else
        log "RESUME: no operator gate_response for iteration $pending_iter — re-dispatching and blocking"
        dispatch_notification "$SEED" "$STATE_DIR" gate_human "$(jq -nc --arg it "$pending_iter" '{iteration:$it, reason:"awaiting_operator_response"}')"
        echo "BLOCKED: awaiting gate_response (iteration $pending_iter)" >&2
        exit 0
      fi
    fi
  fi
else
  log "BOOTSTRAP: no snapshot — initialising state dir"
  mkdir -p "$STATE_DIR/iterations" "$STATE_DIR/gates" "$STATE_DIR/escalations" "$STATE_DIR/logs"
  [[ -f "$STATE_DIR/seed.md" ]] || cp "$SEED" "$STATE_DIR/seed.md"   # seed written once, never modified (§6.1)
fi

# Main role-call loop (§13.1 verbatim body).
# FUP-0800 C.1: run_start — fires once on both bootstrap and resume paths that reach the loop.
event_run_start_resumed=false
[[ -f "$STATE_DIR/state_snapshot.json" ]] && event_run_start_resumed=true
emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "0" "orchestrator" "run_start" "" "" "" \
  "$(jq -nc --arg s "$(basename "$SEED")" --argjson r "$event_run_start_resumed" '{seed:$s, resumed:$r}')"
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
      dispatch_notification "$SEED" "$STATE_DIR" initiative_complete "$(jq -nc '{iteration:"", reason:"completion_predicate_all_passed"}')"
      emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "0" "orchestrator" "run_end" "" "" "" "$(jq -nc '{terminal_reason:"initiative_complete"}')"
      echo "INITIATIVE_COMPLETE"
      exit 0
      ;;
    1)
      : # continue iteration
      ;;
    2)
      log "BUDGET_EXHAUSTED: stop_check returned exit 2"
      dispatch_notification "$SEED" "$STATE_DIR" budget_exhausted "$(jq -nc '{iteration:"", reason:"stop_check_exit_2"}')"
      emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "0" "orchestrator" "run_end" "" "" "" "$(jq -nc '{terminal_reason:"budget_exhausted"}')"
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
    emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "orchestrator" "run_end" "" "" "" "$(jq -nc '{terminal_reason:"max_iterations_exceeded"}')"
    echo "HALT: MAX_ITERATIONS_EXCEEDED" >&2
    exit 6
  fi
  ITER_DIR="$STATE_DIR/iterations/$ITER"
  mkdir -p "$ITER_DIR"
  log "ITERATION $ITER begin"
  # FUP-0800 C.2: iteration_start (capture iter start for the C.5 iteration_end duration).
  EVENT_ITER_T0="$(date +%s%3N 2>/dev/null || echo 0)"
  emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "orchestrator" "iteration_start"
  # FUP-0798: heartbeat UPSERT to workstreams row (operator-awareness for long-running runs).
  # Resilient + non-fatal: no-op when seed.heartbeat.workstream_id absent or env vars unset.
  heartbeat_workstream "$SEED" "$STATE_DIR" "$ITER" "begin"

  # Planner Role Call (FUP-0720: --output-format json + --max-budget-usd via run_claude_json)
  # FUP-0721 (seed schema 1.2): optional planner_model override; CLI default when seed omits.
  # FUP-0800 C.3: planner role_call + role_complete (duration measured across the call, §8.4).
  emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "planner" "role_call"
  EVENT_PL_T0="$(date +%s%3N 2>/dev/null || echo 0)"
  ROLE_MODEL="$(read_seed_field "$SEED" .planner_model 2>/dev/null || echo "")" \
    run_claude_json "$ITER_DIR/planner.json" "/rl-initiative-planner $STATE_DIR $ITER_DIR" \
      2> "$ITER_DIR/planner.stderr"
  # Extract markdown result for any downstream consumer expecting the textual emission:
  jq -r '.result // empty' "$ITER_DIR/planner.json" > "$ITER_DIR/planner.stdout"
  emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "planner" "role_complete" \
    "$(( $(date +%s%3N 2>/dev/null || echo 0) - EVENT_PL_T0 ))"

  # FUP-0768: Planner Path-A (Spec §10.3) — Planner emits INITIATIVE_COMPLETE and writes NO
  # session_plan; terminate clean rather than running plan_review.sh on a missing file.
  # FUP-0790: ALSO require no gate_request_*.json — the bare grep matched the literal
  # "INITIATIVE_COMPLETE" string inside a gate_human escalation narrative (iter 0004:
  # "Neither INITIATIVE_COMPLETE nor a draft plan"), falsely declaring completion. A real
  # Path-A has neither plan nor gates; an escalation has gates but no plan — distinguishable.
  # FUP-0797: defense-in-depth Consumer-confirm — re-evaluate the completion predicate
  # via stop_check.sh BEFORE honouring the Planner's Path-A signal. A false-positive Path-A
  # (Planner emits INITIATIVE_COMPLETE in error with no plan + no gates but registry still
  # open) would otherwise terminate one iteration too early. Mirrors the main-loop-top
  # stop_check.sh invocation pattern (set +e / sc_rc=$?; set -e) and the 4-branch case
  # dispatch on the captured rc (0 confirmed / 1 contradicted / 2 budget / >=3 error).
  if [[ ! -f "$ITER_DIR/session_plan_${ITER}.md" ]] \
     && ! compgen -G "$ITER_DIR/gate_request_${ITER}_*.json" > /dev/null \
     && grep -qF 'INITIATIVE_COMPLETE' "$ITER_DIR/planner.stdout"; then
    log "Planner Path-A signal detected (iteration $ITER); running Consumer-confirm re-verification before dispatch (FUP-0797)"
    set +e
    "$SCRIPT_DIR/hooks/stop_check.sh" "$SEED" "$STATE_DIR"
    cc_rc=$?
    set -e
    case $cc_rc in
      0)
        log "INITIATIVE_COMPLETE: Planner Path-A signal Consumer-confirmed (iteration $ITER; stop_check rc=0)"
        dispatch_notification "$SEED" "$STATE_DIR" initiative_complete "$(jq -nc --arg it "$ITER" '{iteration:$it, reason:"planner_path_a_initiative_complete_consumer_confirmed"}')"
        emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "orchestrator" "run_end" "" "" "" "$(jq -nc '{terminal_reason:"initiative_complete", via:"planner_path_a"}')"
        echo "INITIATIVE_COMPLETE"
        exit 0
        ;;
      1)
        log "BLOCKED: Planner Path-A signal NOT Consumer-confirmed (iteration $ITER; stop_check rc=1 — completion_predicate NOT all-passed)"
        dispatch_notification "$SEED" "$STATE_DIR" gate_human "$(jq -nc --arg it "$ITER" --arg rc "$cc_rc" '{iteration:$it, reason:"planner_path_a_signal_without_consumer_confirm", stop_check_rc:$rc}')"
        echo "BLOCKED: Path-A signal without predicate confirmation (iteration $ITER)" >&2
        exit 0
        ;;
      2)
        log "BUDGET_EXHAUSTED: Consumer-confirm stop_check returned exit 2 (iteration $ITER)"
        dispatch_notification "$SEED" "$STATE_DIR" budget_exhausted "$(jq -nc --arg it "$ITER" '{iteration:$it, reason:"stop_check_exit_2_consumer_confirm"}')"
        echo "BUDGET_EXHAUSTED" >&2
        exit 2
        ;;
      *)
        log "HALT: Consumer-confirm stop_check returned exit $cc_rc (malformed predicate / error per §13.2; iteration $ITER)"
        dispatch_notification "$SEED" "$STATE_DIR" gate_human "$(jq -nc --arg it "$ITER" --arg rc "$cc_rc" '{iteration:$it, reason:"stop_check_error_consumer_confirm", stop_check_rc:$rc}')"
        echo "HALT: STOP_CHECK_ERROR (exit $cc_rc, consumer_confirm)" >&2
        exit 3
        ;;
    esac
  fi

  # Plan review (inner loop; bash-hook-orchestrated, §13.2)
  # FUP-0790: skip plan_review when Planner escalated without a plan — execute_with_gates
  # below routes the gate_request files (broker writes pending_gate + exits 1 -> BLOCKED).
  if [[ -f "$ITER_DIR/session_plan_${ITER}.md" ]]; then
    "$SCRIPT_DIR/hooks/plan_review.sh" "$ITER_DIR/session_plan_${ITER}.md"
  else
    log "ITERATION $ITER Planner escalated without plan — skipping plan_review; routing gate_request(s) via execute_with_gates"
  fi

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
      # §2.5 carve: distinguish gate_human-block (broker wrote pending_gate) vs genuine FAILED iteration.
      # gate_human-block → exit-and-persist (operator answers async + restarts → §6.3 step 3 resume).
      # Genuine FAILED (no pending_gate) → existing continue/re-plan path (bounded by P4-07 fail_count ≥3).
      pending_gate_check="$(jq -r '.pending_gate // empty' "$STATE_DIR/state_snapshot.json" 2>/dev/null || echo "")"
      if [[ -n "$pending_gate_check" && "$pending_gate_check" != "null" ]]; then
        log "ITERATION $ITER BLOCKED on gate_human — pending_gate persisted; awaiting operator gate_response"
        dispatch_notification "$SEED" "$STATE_DIR" gate_human "$(jq -nc --arg it "$ITER" '{iteration:$it, reason:"broker_block_pending_gate"}')"
        # FUP-0833(c): emit run_end on the blocked exit so THIS invocation's run_start (C.1)
        # is paired. Without it a blocked-then-resumed run shows 2 run_start / 1 run_end; the
        # resume invocation emits its own run_start + the terminal run_end, so each invocation
        # is now a balanced run_start/run_end pair.
        emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "0" "orchestrator" "run_end" "" "" "" \
          "$(jq -nc --arg it "$ITER" '{terminal_reason:"blocked_gate_human", iteration:$it}')"
        echo "BLOCKED: pending gate_human (iteration $ITER) — write gate_response_*.json to resume" >&2
        exit 0
      fi
      log "ITERATION $ITER FAILED (execute_with_gates exit 1 — see escalations/)"
      mkdir -p "$STATE_DIR/escalations"
      printf '{"iteration":"%s","classification":"gate_human","reason":"execute_with_gates_exit_1","ts":"%s"}\n' \
             "$ITER" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
             > "$STATE_DIR/escalations/iteration_${ITER}_failed.json"
      dispatch_notification "$SEED" "$STATE_DIR" iteration_failed "$(jq -nc --arg it "$ITER" '{iteration:$it, reason:"execute_with_gates_exit_1"}')"
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
  # FUP-0721 (seed schema 1.2): optional consumer_model override; CLI default when seed omits.
  # FUP-0800 C.4: consumer role_call + phase_complete (the §15 heartbeat-equivalent; once per iteration).
  emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "consumer" "role_call"
  EVENT_CN_T0="$(date +%s%3N 2>/dev/null || echo 0)"
  ROLE_MODEL="$(read_seed_field "$SEED" .consumer_model 2>/dev/null || echo "")" \
    run_claude_json "$ITER_DIR/consumer.json" "/rl-iteration-consumer $STATE_DIR $ITER_DIR"
  emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "consumer" "phase_complete" \
    "$(( $(date +%s%3N 2>/dev/null || echo 0) - EVENT_CN_T0 ))" "" "" \
    "$(jq -nc --argjson cs "$(jq -r '.total_spend_usd // 0' "$RUNNING_SPEND_FILE" 2>/dev/null || echo 0)" '{cumulative_spend:$cs}')"

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
      dispatch_notification "$SEED" "$STATE_DIR" fail_counts_threshold "$(jq -nc --arg it "$ITER" --arg item "$triggered_item" '{iteration:$it, gate_id:$item, reason:"P4_07_fail_counts_ge_3"}')"
      echo "HALT: FAIL_COUNTS_THRESHOLD (item=$triggered_item, iteration=$ITER)" >&2
      exit 3
    fi
  fi

  # FUP-0773: refresh work_registry_hash_at_snapshot in state_snapshot.json post-iter.
  # The Consumer (rl-iteration-consumer) writes pending_gate / state fields but does NOT
  # include work_registry_hash_at_snapshot. The orchestrator's §6.3 step-2 RESUME protection
  # reads .work_registry_hash_at_snapshot + compares against current registry_hash() — HALT
  # on mismatch (registry edited outside orchestrator). Without this write, snap_hash is
  # always empty and the HALT is dormant. Refreshing post-each-iter activates the protection.
  # Idempotent: jq merge adds-or-overwrites the field without touching other snapshot content.
  # Conservative: only fires if state_snapshot.json + WORK_REGISTRY both exist (no-op on
  # bootstrap iter-0001 before Consumer writes snapshot).
  if [[ -f "$STATE_DIR/state_snapshot.json" && -f "$WORK_REGISTRY" ]]; then
    cur_registry_hash="$(registry_hash "$WORK_REGISTRY")"
    jq --arg h "$cur_registry_hash" '.work_registry_hash_at_snapshot = $h' "$STATE_DIR/state_snapshot.json" > "$STATE_DIR/state_snapshot.json.tmp" \
      && mv "$STATE_DIR/state_snapshot.json.tmp" "$STATE_DIR/state_snapshot.json"
  fi

  # Budget check
  "$SCRIPT_DIR/hooks/budget_check.sh" "$SEED" "$STATE_DIR" || { log "BUDGET_EXHAUSTED"; exit 2; }
  log "ITERATION $ITER end"
  # FUP-0800 C.5: iteration_end (duration from the C.2 iteration_start capture).
  emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "orchestrator" "iteration_end" \
    "$(( $(date +%s%3N 2>/dev/null || echo 0) - ${EVENT_ITER_T0:-0} ))"
  # FUP-0798: heartbeat at iteration close so the workstreams row final state reflects the last
  # completed iteration's outcome (not just "begin"; pairs with iter-begin call above).
  heartbeat_workstream "$SEED" "$STATE_DIR" "$ITER" "close"

  # FUP-0815: poll operator-command channel between iterations (after Consumer close + heartbeat,
  # before next iter's stop_check). Dispatches pause / bump_budget / query_register_state.
  command_dispatch_run "$STATE_DIR" "$SEED" "$ITER"
  # Pause-honor: command_dispatch may have written pause_requested.flag; clean-exit if so.
  if [[ -f "$STATE_DIR/pause_requested.flag" ]]; then
    log "PAUSE_REQUESTED: operator pause command honored at iter $ITER boundary"
    dispatch_notification "$SEED" "$STATE_DIR" pause_honored "$(jq -nc --arg it "$ITER" '{iteration:$it, reason:"command_dispatch_pause"}')"
    rm -f "$STATE_DIR/pause_requested.flag"
    # FUP-0833(c): pair this invocation's run_start (C.1) with a run_end on the pause exit too.
    emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "0" "orchestrator" "run_end" "" "" "" \
      "$(jq -nc --arg it "$ITER" '{terminal_reason:"paused", iteration:$it}')"
    echo "PAUSED at iter $ITER boundary (operator command)"
    exit 0
  fi
done
