#!/usr/bin/env bash
# tests/test_fup_0851_0852_failure_and_gate_handling.sh
#
# FUP-0851: on an execute_with_gates exit-1 (deterministic iteration failure), the orchestrator
#   previously `continue`d straight to the loop top — skipping (a) the Consumer that owns
#   fail_counts increments (so the P4-07 >=3 guard never fired) and (b) the end-of-loop
#   command_dispatch (so an operator `pause` was never honored). A blocked item looped to
#   MAX_ITERATIONS and could not be paused. Fix: increment fail_counts + escalate at >=3 + poll
#   the command channel on the exit-1 path.
#   A: pause command pre-written -> orchestrator PAUSES on the failed iteration (rc 0), not loops.
#   B: no pause, item fails every iteration -> HALT FAIL_COUNTS_THRESHOLD (rc 3) by iter 3, not MAX_ITERATIONS.
#
# FUP-0852: execute_with_gates pass-2 inlined a deferred gate_human on FILE EXISTENCE alone, so a
#   malformed/invalid (or self-escalated) Answerer gate_response was re-admitted as "resolved" and
#   the gate was silently bypassed. Fix: require a well-formed FR-008 response + no escalation artefact.
#   C: a gate_human with a present-but-MALFORMED gate_response must BLOCK (exit 1 + pending_gate), not inline.
#
# Run: bash tests/test_fup_0851_0852_failure_and_gate_handling.sh   (exit 0 = all PASS)

set -uo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RALPH_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
PASS=0; FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*" >&2; FAIL=$((FAIL+1)); }

# ---------- shared: build a sandbox that runs the REAL orchestrator with a FAILING Executor ----------
# mock claude: planner writes a plan with target_item_id=FAIL-001; Executor (--print) emits a
# schema-valid execution_result with terminal_reason=irregular_termination -> execute_with_gates exit 1.
build_sandbox() {
  local root="$1" iters_max="$2"
  mkdir -p "$root/bin"
  cat > "$root/bin/claude" <<'SHIM'
#!/usr/bin/env bash
PROMPT="$*"
if echo "$PROMPT" | grep -qw -- '--print'; then
  cat > /dev/null
  cat <<EOF
{"session_id":"mock","result":"mock executor FAIL","total_cost_usd":0.0,"permission_denials":[],"terminal_reason":"irregular_termination"}
EOF
  exit 0
fi
LAST_ARG="${!#}"; ITER_DIR="${LAST_ARG##* }"; [[ "$ITER_DIR" != */iterations/* ]] && ITER_DIR="."
mkdir -p "$ITER_DIR" 2>/dev/null || true; ITER_NUM="$(basename "$ITER_DIR")"
if echo "$PROMPT" | grep -q '/rl-initiative-planner'; then
  cat > "$ITER_DIR/session_plan_${ITER_NUM}.md" <<EOF
---
iteration_index: ${ITER_NUM}
shape: noop
max_turns: 1
target_item_id: FAIL-001
---
# Mock plan (always-failing executor)
Findings: 0 BLOCKER, 0 DRIFT
EOF
  echo '{"result":"mock planner output (no INITIATIVE_COMPLETE)","total_cost_usd":0.0}'
elif echo "$PROMPT" | grep -q '/rl-iteration-consumer'; then
  echo '{"result":"mock consumer output","total_cost_usd":0.0}'
else
  echo '{"session_id":"mock","result":"mock unknown","total_cost_usd":0.0,"permission_denials":[],"terminal_reason":"completed"}'
fi
SHIM
  chmod +x "$root/bin/claude"
  # No-op win11toast stub: notify.sh's fallback prefers a PATH `win11toast` binary over the
  # python module, so this keeps the test from firing (blocking) real desktop toasts.
  printf '#!/usr/bin/env bash\nexit 0\n' > "$root/bin/win11toast"; chmod +x "$root/bin/win11toast"
  # hooks overlay: stub plan_review (trivially accept); real stop_check/execute_with_gates/budget_check
  mkdir -p "$root/hooks"
  cat > "$root/hooks/plan_review.sh" <<'HK'
#!/usr/bin/env bash
echo "mock plan_review: accepted $1" >&2
exit 0
HK
  chmod +x "$root/hooks/plan_review.sh"
  for h in stop_check.sh execute_with_gates.sh budget_check.sh; do cp "$RALPH_ROOT/hooks/$h" "$root/hooks/$h"; done
  mkdir -p "$root/ws"
  cat > "$root/ws/registry.md" <<'REG'
# reg

| ID | Status | Title |
|---|---|---|
| FAIL-001 | open | always-failing item |
REG
  cat > "$root/seed.md" <<EOF
---
seed_schema_version: 1.3
initiative: { slug: t0851, title: t, owner: t, description: t }
workspace_root: "$root/ws"
state_dir_relative: "state/"
work_registry: "registry.md"
read_only_paths: []
context_documents: []
target_order: [FAIL-001]
session_shape_catalog: [ { name: noop, template_pointer: "prompt_key:rl.session_shape.noop" } ]
verification_bindings: []
verification_policy: inline_per_session_plan
mcp_servers: []
budget: { tokens_usd: 100.0, iterations_max: $iters_max }
permission_posture: "--permission-mode auto"
completion_predicate:
  - name: registry_drained
    check_kind: registry_zero_open
    params: { path: "registry.md", filter: "Status != closed" }
---
EOF
  mkdir -p "$root/ws/state/commands"
  echo '{"total_spend_usd":0.0}' > "$root/ws/state/spend.json"
  # ralph overlay: real lib/schemas, our hooks, real orchestrator
  mkdir -p "$root/ralph"
  ln -s "$RALPH_ROOT/lib" "$root/ralph/lib"; ln -s "$RALPH_ROOT/schemas" "$root/ralph/schemas"; ln -s "$root/hooks" "$root/ralph/hooks"
  cp "$RALPH_ROOT/orchestrator.sh" "$root/ralph/orchestrator.sh"
}
run_orch() {
  local root="$1"
  PATH="$root/bin:$PATH" CLAUDE_SKILLS_DIR="$RALPH_ROOT" \
    bash -c "unset MSYS_NO_PATHCONV; exec bash \"$root/ralph/orchestrator.sh\" \"$root/seed.md\"" \
    > "$root/orch.out" 2> "$root/orch.err"
  echo $?
}

# ---------- A: pause honored on a failed iteration (FUP-0851) ----------
A="$(mktemp -d)"; build_sandbox "$A" 5
PID="pause_$(date -u +%s)"
echo "{\"command_type\":\"pause\",\"command_id\":\"$PID\",\"issued_by\":\"t\",\"issued_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"reason\":\"t\"}" > "$A/ws/state/commands/${PID}.json"
RC=$(run_orch "$A")
if [[ "$RC" == "0" ]] && grep -q 'PAUSED at iter' "$A/orch.out"; then pass "[A] pause honored on failed iteration (rc 0, PAUSED — not looped to MAX_ITERATIONS)"
else fail "[A] expected rc0+PAUSED, got rc=$RC :: $(tail -3 "$A/orch.err")"; fi
FCA=$(jq -r 'map(select(.item_id=="FAIL-001"))|.[0].count//0' "$A/ws/state/fail_counts.json" 2>/dev/null)
[[ "${FCA:-0}" -ge 1 ]] && pass "[A] fail_counts[FAIL-001]=$FCA incremented on exit-1 (Consumer was skipped)" || fail "[A] fail_counts not incremented (got ${FCA:-none})"
rm -rf "$A"

# ---------- B: >=3 fail-count escalation instead of MAX_ITERATIONS (FUP-0851) ----------
B="$(mktemp -d)"; build_sandbox "$B" 10
RC=$(run_orch "$B")
if [[ "$RC" == "3" ]] && grep -q 'FAIL_COUNTS_THRESHOLD' "$B/orch.err"; then pass "[B] HALT FAIL_COUNTS_THRESHOLD at >=3 (rc 3 — not run to MAX_ITERATIONS=10)"
else fail "[B] expected rc3+FAIL_COUNTS_THRESHOLD, got rc=$RC :: $(tail -3 "$B/orch.err")"; fi
FCB=$(jq -r 'map(select(.item_id=="FAIL-001"))|.[0].count//0' "$B/ws/state/fail_counts.json" 2>/dev/null)
[[ "${FCB:-0}" -ge 3 ]] && pass "[B] fail_counts[FAIL-001]=$FCB reached threshold" || fail "[B] fail_counts=$FCB < 3"
grep -q 'MAX_ITERATIONS' "$B/orch.err" && fail "[B] regressed: hit MAX_ITERATIONS (runaway not stopped)" || pass "[B] did NOT run to MAX_ITERATIONS"
rm -rf "$B"

# ---------- C: malformed gate_response must BLOCK, not inline (FUP-0852) ----------
C="$(mktemp -d)"; mkdir -p "$C/ws/state/iterations/0001"
cat > "$C/seed.md" <<EOF
---
initiative: { slug: t0852 }
workspace_root: "$C/ws"
permission_posture: "--permission-mode auto"
budget: { tokens_usd: 5.0 }
read_only_paths: []
mcp_servers: []
gate_policy:
  pre_classification:
    - { pattern: "cluster:force-human", class: gate_human }
---
EOF
ID="$C/ws/state/iterations/0001"
# a gate_human-classified gate_request + a MALFORMED gate_response (unescaped backslash) sitting beside it
printf '{"gate_id":"g1","cluster":"force-human","question_text":"q","options":[{"id":"A","label":"opt a"},{"id":"B","label":"opt b"}]}\n' > "$ID/gate_request_0001_0001.json"
printf '{"gate_id":"g1","selected_option":"A","reasoning":"path C:\\Users\\x bad escape","confidence":0.9,"classification_check":"x"}\n' > "$ID/gate_response_0001_0001.json"
# a minimal valid session plan so execute_with_gates reaches the gate pass (no Executor run on block)
printf -- '---\niteration_index: 0001\n---\nplan\n' > "$ID/session_plan_0001.md"
mkdir -p "$C/bin"; printf '#!/usr/bin/env bash\nexit 0\n' > "$C/bin/win11toast"; chmod +x "$C/bin/win11toast"
set +e
PATH="$C/bin:$PATH" CLAUDE_SKILLS_DIR="$RALPH_ROOT" bash "$RALPH_ROOT/hooks/execute_with_gates.sh" "$C/seed.md" "$ID" > "$C/ewg.out" 2> "$C/ewg.err"
EWG_RC=$?
set -e
if [[ "$EWG_RC" == "1" ]] && [[ -f "$C/ws/state/state_snapshot.json" ]] && jq -e '.pending_gate' "$C/ws/state/state_snapshot.json" >/dev/null 2>&1; then
  pass "[C] malformed gate_response BLOCKED (exit 1 + pending_gate written) — not silently inlined"
else
  fail "[C] expected exit1+pending_gate, got rc=$EWG_RC :: $(grep -iE 'inlined|escalat|FUP-0852' "$C/ewg.err" | tail -2)"
fi
grep -q 'FUP-0852' "$C/ewg.err" && pass "[C] emitted the FUP-0852 invalid-response diagnostic" || echo "NOTE: [C] FUP-0852 diagnostic line not found (non-fatal)"
rm -rf "$C"

echo ""
echo "=== test_fup_0851_0852_failure_and_gate_handling.sh: $PASS PASS, $FAIL FAIL ==="
[[ $FAIL -eq 0 ]]
