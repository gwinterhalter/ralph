#!/usr/bin/env bash
# tests/test_ralph_loop_edge_conditions.sh
#
# Comprehensive coverage of the ralph-loop (orchestrator.sh + hooks) TERMINAL / EDGE paths that the
# existing suite leaves untested. Drives the REAL orchestrator with a parameterised mock `claude`
# shim (zero real API spend). Complements: 0815 (pause), 0851/0852 (fail-threshold + malformed gate),
# 0774 (gate demote/defer), 0813 (descriptive sentinel), 0930_0931 (report-recovery + narrative).
#
# Scenarios:
#   S1  COMPLETE            — happy drain: item closes -> stop_check registry_zero_open -> rc0 INITIATIVE_COMPLETE
#   S2  MAX_ITERATIONS      — item never closes, iters_max=2 -> rc6 MAX_ITERATIONS_EXCEEDED
#   S3  BUDGET_EXHAUSTED    — spend > cap -> stop_check(budget_check) -> rc2 BUDGET_EXHAUSTED
#   S4  REGISTRY_HASH       — resume snapshot hash != current registry -> rc3 REGISTRY_HASH_MISMATCH
#   S5  STOP_CHECK_ERROR    — malformed completion_predicate -> stop_check rc>=3 -> rc3 STOP_CHECK_ERROR
#   S6  GATE_RESUME (FUP-0932) — planner-escalated gate (no plan) -> answer -> resume RE-PLANS -> rc0 COMPLETE
#   S7  PATHA_GUARD (FUP-0768) — planner emits INITIATIVE_COMPLETE while registry OPEN -> must NOT falsely complete
#
# Run: bash tests/test_ralph_loop_edge_conditions.sh   (exit 0 = all PASS)

set -uo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RALPH_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
PASS=0; FAIL=0
pass() { echo "  PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $*" >&2; FAIL=$((FAIL+1)); }

# ---- shared sandbox: real orchestrator + hooks + a mock claude driven by $root/mock.env ----
# build_sandbox <root> <iters_max> <budget_usd> [extra seed yaml lines...]
build_sandbox() {
  local root="$1" iters_max="$2" budget="$3"; shift 3
  local extra="$*"
  mkdir -p "$root/bin" "$root/hooks" "$root/ws/state/commands" "$root/ralph"
  # --- mock claude (behaviour flags sourced from $MOCK_ENV) ---
  cat > "$root/bin/claude" <<'SHIM'
#!/usr/bin/env bash
[[ -n "${MOCK_ENV:-}" && -f "${MOCK_ENV:-}" ]] && source "$MOCK_ENV"
PROMPT="$*"
LAST_ARG="${!#}"
ITER_DIR="${LAST_ARG##* }"; [[ "$ITER_DIR" != */iterations/* ]] && ITER_DIR="."
REST="${LAST_ARG% *}"; STATE_DIR="${REST##* }"
ITER_NUM="$(basename "$ITER_DIR")"
mkdir -p "$ITER_DIR" 2>/dev/null || true

if echo "$PROMPT" | grep -qw -- '--print'; then
  cat > /dev/null   # drain the executor prompt on stdin
  # Optional: write outside the sandbox to trip the read-only scan (S-read-only; unused here).
  [[ -n "${MOCK_EXEC_WRITE_OUTSIDE:-}" ]] && echo "violation" > "$MOCK_EXEC_WRITE_OUTSIDE" 2>/dev/null || true
  printf '{"session_id":"mock","result":"mock exec","total_cost_usd":%s,"permission_denials":[],"terminal_reason":"%s"}\n' "${MOCK_EXEC_COST:-0.0}" "${MOCK_EXEC_TERMINAL:-completed}"
  exit 0
fi

if echo "$PROMPT" | grep -q '/rl-initiative-planner'; then
  case "${MOCK_PLANNER:-plan}" in
    path_a)
      echo '{"result":"work is done. INITIATIVE_COMPLETE","total_cost_usd":0.0}' ;;   # no session_plan
    gate_until_answered)
      # gate on the FIRST encounter (no prior gate_response), plan once answered.
      if find "$STATE_DIR" -name 'gate_response_*.json' 2>/dev/null | grep -q .; then
        cat > "$ITER_DIR/session_plan_${ITER_NUM}.md" <<PLN
---
iteration_index: ${ITER_NUM}
shape: noop
target_item_id: ITEM-001
---
# mock plan (post-gate)
PLN
        echo '{"result":"planned post-gate","total_cost_usd":0.0}'
      else
        cat > "$ITER_DIR/gate_request_${ITER_NUM}_0001.json" <<GRQ
{"gate_id":"tone-choice-01","cluster":"force-human","question_text":"tone-choice: pick","options":[{"id":"A","label":"opt a"},{"id":"B","label":"opt b"}]}
GRQ
        echo '{"result":"escalated gate (no plan)","total_cost_usd":0.0}'
      fi ;;
    plan|*)
      cat > "$ITER_DIR/session_plan_${ITER_NUM}.md" <<PLN
---
iteration_index: ${ITER_NUM}
shape: noop
target_item_id: ITEM-001
---
# mock plan
PLN
      echo '{"result":"planned (no INITIATIVE_COMPLETE)","total_cost_usd":0.0}' ;;
  esac
  exit 0
fi

if echo "$PROMPT" | grep -q '/rl-iteration-consumer'; then
  if [[ "${MOCK_CONSUMER_CLOSE:-0}" == "1" && -n "${MOCK_REGISTRY:-}" && -f "${MOCK_REGISTRY:-}" ]]; then
    sed -i 's/| open |/| closed |/g' "$MOCK_REGISTRY" 2>/dev/null || true
  fi
  echo '{"result":"mock consumer","total_cost_usd":0.0}'
  exit 0
fi

echo '{"session_id":"mock","result":"mock unknown","total_cost_usd":0.0,"permission_denials":[],"terminal_reason":"completed"}'
exit 0
SHIM
  chmod +x "$root/bin/claude"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$root/bin/win11toast"; chmod +x "$root/bin/win11toast"
  # hooks: stub plan_review (accept); real stop_check / execute_with_gates / budget_check
  printf '#!/usr/bin/env bash\nexit 0\n' > "$root/hooks/plan_review.sh"; chmod +x "$root/hooks/plan_review.sh"
  for h in stop_check.sh execute_with_gates.sh budget_check.sh; do cp "$RALPH_ROOT/hooks/$h" "$root/hooks/$h"; done
  # registry: | ID | Status | Title | (one open item)
  cat > "$root/ws/registry.md" <<'REG'
# reg

| ID | Status | Title |
|---|---|---|
| ITEM-001 | open | the one item |
REG
  cat > "$root/seed.md" <<EOF
---
seed_schema_version: 1.3
initiative: { slug: edge, title: t, owner: t, description: t }
workspace_root: "$root/ws"
state_dir_relative: "state/"
work_registry: "registry.md"
read_only_paths: []
context_documents: []
target_order: [ITEM-001]
session_shape_catalog: [ { name: noop, template_pointer: "prompt_key:rl.session_shape.noop" } ]
verification_bindings: []
verification_policy: inline_per_session_plan
mcp_servers: []
budget: { tokens_usd: $budget, iterations_max: $iters_max }
permission_posture: "--permission-mode auto"
$extra
---
EOF
  echo '{"total_spend_usd":0.0}' > "$root/ws/state/spend.json"
  ln -s "$RALPH_ROOT/lib" "$root/ralph/lib"; ln -s "$RALPH_ROOT/schemas" "$root/ralph/schemas"; ln -s "$root/hooks" "$root/ralph/hooks"
  cp "$RALPH_ROOT/orchestrator.sh" "$root/ralph/orchestrator.sh"
}
# default completion predicate (Status-column filter form)
CP_FILTER='completion_predicate:
  - name: registry_drained
    check_kind: registry_zero_open
    params: { path: "registry.md", filter: "Status != closed" }'

run_orch() {  # <root>  -> echoes rc; writes orch.out/orch.err; MOCK_ENV pre-set by caller
  local root="$1"
  PATH="$root/bin:$PATH" CLAUDE_SKILLS_DIR="$RALPH_ROOT" MOCK_ENV="$root/mock.env" \
    bash -c "unset MSYS_NO_PATHCONV; exec bash \"$root/ralph/orchestrator.sh\" \"$root/seed.md\"" \
    > "$root/orch.out" 2> "$root/orch.err"
  echo $?
}

# =========================== S1 — happy drain to INITIATIVE_COMPLETE ===========================
echo "S1 — happy drain -> INITIATIVE_COMPLETE (stop_check registry_zero_open)"
R="$(mktemp -d)"; build_sandbox "$R" 5 100.0 "$CP_FILTER"
printf 'MOCK_PLANNER=plan\nMOCK_CONSUMER_CLOSE=1\nMOCK_REGISTRY=%q\n' "$R/ws/registry.md" > "$R/mock.env"
RC=$(run_orch "$R")
{ [[ "$RC" == "0" ]] && grep -q 'INITIATIVE_COMPLETE' "$R/orch.out"; } \
  && pass "S1 rc0 + INITIATIVE_COMPLETE (item closed, predicate passed)" \
  || fail "S1 expected rc0+INITIATIVE_COMPLETE, got rc=$RC :: $(tail -2 "$R/orch.err")"
grep -q '| closed |' "$R/ws/registry.md" && pass "S1 registry item was closed by the Consumer" || fail "S1 item not closed"
rm -rf "$R"

# =========================== S2 — MAX_ITERATIONS_EXCEEDED ===========================
echo "S2 — item never closes, iters_max=2 -> iteration cap enforced, no runaway (rc2)"
R="$(mktemp -d)"; build_sandbox "$R" 2 100.0 "$CP_FILTER"
printf 'MOCK_PLANNER=plan\nMOCK_CONSUMER_CLOSE=0\n' > "$R/mock.env"   # consumer never closes -> never complete
RC=$(run_orch "$R")
# The iteration cap is enforced by budget_check (exit1 -> stop_check exit2 -> orchestrator rc2); the
# orchestrator's own MAX_ITERATIONS_EXCEEDED (rc6) is a secondary backstop budget_check pre-empts.
{ [[ "$RC" == "2" ]] && grep -q 'iterations_max reached' "$R/orch.err"; } \
  && pass "S2 rc2 + 'iterations_max reached' (a never-closing item is stopped at the cap, not runaway)" \
  || fail "S2 expected rc2+iterations_max reached, got rc=$RC :: $(tail -2 "$R/orch.err")"
rm -rf "$R"

# =========================== S3 — BUDGET_EXHAUSTED ===========================
echo "S3 — executor cost sums over tokens_usd cap -> BUDGET_EXHAUSTED (rc2)"
# budget_check sums total_cost_usd from iterations/NNNN/execution_result_NNNN.json (not spend.json).
# One iteration at \$5 exceeds the \$1 cap; iters_max is high so the tokens ceiling fires first.
R="$(mktemp -d)"; build_sandbox "$R" 20 1.0 "$CP_FILTER"
printf 'MOCK_PLANNER=plan\nMOCK_CONSUMER_CLOSE=0\nMOCK_EXEC_COST=5.0\n' > "$R/mock.env"
RC=$(run_orch "$R")
{ [[ "$RC" == "2" ]] && grep -q 'tokens_usd ceiling exceeded' "$R/orch.err"; } \
  && pass "S3 rc2 + 'tokens_usd ceiling exceeded' (cumulative spend over cap halts the loop)" \
  || fail "S3 expected rc2+tokens_usd ceiling exceeded, got rc=$RC :: $(tail -2 "$R/orch.err")"
rm -rf "$R"

# =========================== S4 — REGISTRY_HASH_MISMATCH (resume integrity) ===========================
echo "S4 — resume snapshot hash != current registry -> REGISTRY_HASH_MISMATCH (rc3)"
R="$(mktemp -d)"; build_sandbox "$R" 5 100.0 "$CP_FILTER"
printf 'MOCK_PLANNER=plan\nMOCK_CONSUMER_CLOSE=0\n' > "$R/mock.env"
printf '{"work_registry_hash_at_snapshot":"deadbeefdeadbeef"}\n' > "$R/ws/state/state_snapshot.json"
RC=$(run_orch "$R")
{ [[ "$RC" == "3" ]] && grep -q 'REGISTRY_HASH_MISMATCH' "$R/orch.err"; } \
  && pass "S4 rc3 + REGISTRY_HASH_MISMATCH (registry edited outside the orchestrator is caught)" \
  || fail "S4 expected rc3+REGISTRY_HASH_MISMATCH, got rc=$RC :: $(tail -2 "$R/orch.err")"
rm -rf "$R"

# =========================== S5 — STOP_CHECK_ERROR (malformed predicate) ===========================
echo "S5 — malformed completion_predicate -> stop_check rc>=3 -> STOP_CHECK_ERROR (rc3)"
R="$(mktemp -d)"
# predicate name is not a known sub-evaluator AND no params.filter -> stop_check exit 3
build_sandbox "$R" 5 100.0 'completion_predicate:
  - name: not_a_known_evaluator
    check_kind: registry_zero_open
    params: { path: "registry.md" }'
printf 'MOCK_PLANNER=plan\nMOCK_CONSUMER_CLOSE=0\n' > "$R/mock.env"
RC=$(run_orch "$R")
{ [[ "$RC" == "3" ]] && grep -q 'STOP_CHECK_ERROR' "$R/orch.err"; } \
  && pass "S5 rc3 + STOP_CHECK_ERROR (a malformed predicate halts instead of looping)" \
  || fail "S5 expected rc3+STOP_CHECK_ERROR, got rc=$RC :: $(tail -2 "$R/orch.err")"
rm -rf "$R"

# =========================== S6 — GATE-RESUME, planner-escalated (FUP-0932) ===========================
echo "S6 — planner-escalated gate (no plan) -> answer -> resume RE-PLANS -> INITIATIVE_COMPLETE (FUP-0932)"
R="$(mktemp -d)"
build_sandbox "$R" 5 100.0 "$CP_FILTER
gate_policy:
  pre_classification:
    - { pattern: \"contains:tone-choice\", class: gate_human }"
printf 'MOCK_PLANNER=gate_until_answered\nMOCK_CONSUMER_CLOSE=1\nMOCK_REGISTRY=%q\n' "$R/ws/registry.md" > "$R/mock.env"
# Run 1: planner raises the gate_human, orchestrator BLOCKS (rc0) with pending_gate.
RC1=$(run_orch "$R")
GATE_REQ="$(find "$R/ws/state/iterations" -name 'gate_request_*_0001.json' 2>/dev/null | head -1)"
{ [[ "$RC1" == "0" ]] && grep -q 'BLOCKED' "$R/orch.err" && [[ -n "$GATE_REQ" ]] \
    && jq -e '.pending_gate' "$R/ws/state/state_snapshot.json" >/dev/null 2>&1; } \
  && pass "S6a run1: gate_human raised + BLOCKED + pending_gate persisted" \
  || fail "S6a expected block+pending_gate, rc1=$RC1 :: $(tail -2 "$R/orch.err")"
# Operator answers async: drop a schema-valid gate_response beside the gate_request.
if [[ -n "$GATE_REQ" ]]; then
  GR_DIR="$(dirname "$GATE_REQ")"; SUF="$(basename "$GATE_REQ" .json)"; SUF="${SUF#gate_request_}"
  printf '{"gate_id":"tone-choice-01","selected_option":"A","reasoning":"operator picks A","confidence":1.0,"classification_check":"operator_response"}\n' \
    > "$GR_DIR/gate_response_${SUF}.json"
fi
# Run 2: resume must NOT crash (the old bug) — it re-plans, drains, completes.
RC2=$(run_orch "$R")
{ [[ "$RC2" == "0" ]] && grep -q 'INITIATIVE_COMPLETE' "$R/orch.out"; } \
  && pass "S6b run2: resume re-planned + drained -> rc0 INITIATIVE_COMPLETE (FUP-0932 fixed)" \
  || fail "S6b expected rc0+INITIATIVE_COMPLETE, got rc2=$RC2 :: $(tail -3 "$R/orch.err")"
grep -q 'READ_ONLY_BOUNDARY_VIOLATION' "$R/orch.err" \
  && fail "S6b regressed: the pre-FUP-0932 spurious READ_ONLY HALT reappeared" \
  || pass "S6b no spurious READ_ONLY_BOUNDARY_VIOLATION on resume"
grep -q 're-plans with the inlined answer (FUP-0932)' "$R/orch.err" \
  && pass "S6b took the FUP-0932 re-plan branch" || echo "  NOTE: S6b FUP-0932 log line not found (non-fatal)"
rm -rf "$R"

# =========================== S7 — Path-A false-completion guard (FUP-0768) ===========================
echo "S7 — planner emits INITIATIVE_COMPLETE while registry OPEN -> must NOT falsely complete (FUP-0768)"
R="$(mktemp -d)"; build_sandbox "$R" 2 100.0 "$CP_FILTER"
printf 'MOCK_PLANNER=path_a\nMOCK_CONSUMER_CLOSE=0\n' > "$R/mock.env"   # registry stays OPEN
RC=$(run_orch "$R")
# The guard: a planner INITIATIVE_COMPLETE signal that the Consumer-confirm stop_check cannot confirm
# (registry still open) must NOT terminate as a clean completion. Acceptable: escalate (rc0 via the
# signal-without-confirm path) or run to MAX_ITERATIONS — but NEVER a clean INITIATIVE_COMPLETE.
if grep -q 'INITIATIVE_COMPLETE: Planner Path-A signal Consumer-confirmed' "$R/orch.err"; then
  fail "S7 FALSE COMPLETION: declared complete with the registry still open (guard failed)"
else
  pass "S7 did NOT falsely complete on an unconfirmed Planner Path-A signal (guard held)"
fi
rm -rf "$R"

echo ""
echo "=== test_ralph_loop_edge_conditions.sh: $PASS PASS, $FAIL FAIL ==="
[[ $FAIL -eq 0 ]]
