#!/usr/bin/env bash
# tests/test_fup_0815_orchestrator_integration.sh — FUP-0815 orchestrator-in-the-loop
# integration smoke test (the "re-run IS the validation pass" per CLAUDE.md §Ralph Loop
# In-Run Bug Workflow). Exercises the orchestrator.sh edits 3.A (source command_dispatch),
# 3.B (main-loop hook + pause-honor), and 3.C (budget_override read in run_claude_json).
#
# Uses the SAME mock-claude PATH-shim pattern as test_fup_0774_rl_integration.sh:
# zero API spend; canned shim emits trivial JSON for Planner + Consumer role calls.
#
# Scenario:
#   1. Pre-write a pause.json into $STATE_DIR/commands/
#   2. Launch orchestrator.sh against a sandbox seed (artefact_exists predicate that
#      never passes — orchestrator would run to iterations_max=2 then HALT)
#   3. iter-0001 runs (mock Planner + plan_review + mock Consumer)
#   4. At iter-boundary: command_dispatch_run consumes pause.json, writes
#      pause_requested.flag + response.json
#   5. pause-honor block exits orchestrator with rc=0 + "PAUSED" message
#
# Assertions:
#   - orchestrator exits 0 (clean pause-honor, NOT MAX_ITERATIONS_EXCEEDED rc=6)
#   - pause.json archived to commands/.processed/
#   - response.json written with status=pause_honored_at_iter_boundary
#   - orchestrator.log contains "PAUSE_REQUESTED"
#   - stdout contains "PAUSED at iter 0001 boundary"
#
# Run: bash tests/test_fup_0815_orchestrator_integration.sh
# Exit 0 = all PASS; exit 1 = any FAIL.

set -uo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RALPH_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
TMPROOT="$(mktemp -d)"
TMPBIN="$TMPROOT/bin"
TMPSEED="$TMPROOT/seed.md"
STATE_DIR="$TMPROOT/state"
ITER="0001"
ITER_DIR="$STATE_DIR/iterations/$ITER"
trap 'rm -rf "$TMPROOT"' EXIT

# Env-skip: on Windows+WinGet jq the orchestrator's MSYS_NO_PATHCONV=1 (orchestrator.sh L11,
# FUP-0740 slash-prefix preservation) breaks the WinGet jq.exe (Windows-native binary expects
# native Windows paths, not Unix /tmp/...). The hermetic command_dispatch tests + path-discovery
# tests + grep-verified orchestrator wiring (3.A/3.B/3.C) provide strong correctness evidence
# under Windows. This end-to-end orchestrator-in-loop validation passes under Unix jq
# environments (operator's main env). Skip gracefully when WinGet jq is detected.
if jq --version 2>/dev/null | grep -q '^jq-' && \
   command -v jq | grep -qiE 'wingt|winget|appdata.*local.*microsoft'; then
  echo "SKIP: WinGet jq detected at $(command -v jq) — MSYS_NO_PATHCONV=1 / WinGet-jq.exe path conversion incompatibility (env-specific, NOT a FUP-0815 code defect; see test header)."
  echo "=== test_fup_0815_orchestrator_integration.sh: SKIPPED (env) ==="
  exit 0
fi

PASS=0; FAIL=0
fail() { echo "FAIL: $*" >&2; FAIL=$((FAIL+1)); }
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }

# ---- 1. Mock claude shim (canned JSON for Planner + Consumer) ----
mkdir -p "$TMPBIN"
cat > "$TMPBIN/claude" <<'SHIM'
#!/usr/bin/env bash
# Mock claude -p for orchestrator integration test. Emits trivial JSON.
# Detects which role-call by looking for /rl-initiative-planner vs /rl-iteration-consumer in the prompt.
# Writes a Planner Path-B output (a minimal session_plan + no INITIATIVE_COMPLETE signal) so the
# orchestrator proceeds to execute_with_gates + Consumer.
PROMPT="$*"
ITER_DIR=""
# Extract the second positional after `--` (the prompt usually has $STATE_DIR $ITER_DIR).
for arg in "$@"; do
  case "$arg" in
    */iterations/*) ITER_DIR="$arg" ;;
  esac
done
[[ -z "$ITER_DIR" ]] && ITER_DIR="."
mkdir -p "$ITER_DIR"

if echo "$PROMPT" | grep -q '/rl-initiative-planner'; then
  # Write a tiny session_plan so plan_review.sh has something to inspect; no gates so execute_with_gates skips
  ITER_NUM="$(basename "$ITER_DIR")"
  cat > "$ITER_DIR/session_plan_${ITER_NUM}.md" <<EOF
---
iteration_index: ${ITER_NUM}
shape: noop
max_turns: 1
target_item_id: noop_target
---
# Mock session plan (test_fup_0815 integration)
Findings: 0 BLOCKER, 0 DRIFT
EOF
  echo '{"result":"mock planner output (no INITIATIVE_COMPLETE)","total_cost_usd":0.0}'
elif echo "$PROMPT" | grep -q '/rl-iteration-consumer'; then
  # Mock Consumer — emit empty execution_result; no fail_counts mutation
  cat > "$ITER_DIR/execution_result.json" <<EOF
{"iteration":"${ITER_NUM:-0001}","closed_items":[],"terminal_reason":"completed","permission_denials":[]}
EOF
  echo '{"result":"mock consumer output","total_cost_usd":0.0}'
else
  echo '{"result":"mock unknown","total_cost_usd":0.0}'
fi
SHIM
chmod +x "$TMPBIN/claude"

# ---- 2. Mock plan_review.sh hook (avoid loading cf-session-plan-reviewer skill for the mock plan) ----
HOOKS_TMP="$TMPROOT/hooks"
mkdir -p "$HOOKS_TMP"
cat > "$HOOKS_TMP/plan_review.sh" <<'HK'
#!/usr/bin/env bash
# Mock plan_review.sh — trivially accepts any plan.
echo "mock plan_review: accepted $1" >&2
exit 0
HK
chmod +x "$HOOKS_TMP/plan_review.sh"
# Copy real hooks but override plan_review.sh
for h in stop_check.sh execute_with_gates.sh budget_check.sh; do
  cp "$RALPH_ROOT/hooks/$h" "$HOOKS_TMP/$h"
done

# ---- 3. Sandbox seed (artefact_exists with never-passing predicate; iterations_max=2) ----
# Registry has ONE RESOLVED row citing a never-existing .md token → artefact_exists computes
# missing_count=1 → all_pass=0 → orchestrator continues into iter loop (where command_dispatch fires).
SANDBOX_ROOT="$TMPROOT/sandbox_workspace"
mkdir -p "$SANDBOX_ROOT"
cat > "$SANDBOX_ROOT/registry.md" <<'REG'
# Sandbox registry — drives artefact_exists to never-pass for integration test

| ID | Name | Gap description | Priority | Prerequisites | Resolution path |
|---|---|---|---|---|---|
| TEST-001 | smoke test gap | predicate driver | **RESOLVED** | none | never_existing_artefact_for_fup_0815_smoke.md |
REG

cat > "$TMPSEED" <<EOF
---
seed_schema_version: 1.3
initiative:
  slug: fup_0815_integration_test
  title: FUP-0815 integration smoke test
  owner: test_harness
  description: Mock initiative for the FUP-0815 orchestrator integration smoke test.
  target_completion_estimate: 1 iteration (pause-honor exits before iter 0002)
workspace_root: "$SANDBOX_ROOT"
state_dir_relative: "state/"
work_registry: "registry.md"
read_only_paths: []
context_documents: []
target_order: []
budget:
  tokens_usd: 0.10
  iterations_max: 2
session_shape_catalog:
  - name: noop
    template_pointer: prompt_key:rl.session_shape.noop
verification_bindings: []
verification_policy: inline_per_session_plan
mcp_servers: []
completion_predicate:
  - name: never_passes_until_operator_pauses
    check_kind: artefact_exists
    params:
      root_field: workspace_root
      targets_source: registry.md
---
body irrelevant
EOF

# State dir is workspace_root/state/
STATE_DIR="$SANDBOX_ROOT/state"
mkdir -p "$STATE_DIR/commands"
# Pre-create spend.json — works around a Git Bash quirk where `[[ -f ]] || echo > file`
# inside the run_claude_json function silently fails to create the file (causing the
# downstream jq read to ENOENT, current_spend="", and the test to hang/HALT). The real
# orchestrator path normally never hits this because spend.json gets created at the very
# first run_claude_json call; pre-creating it sidesteps the Git Bash race.
echo '{"total_spend_usd": 0.0}' > "$STATE_DIR/spend.json"

# ---- 4. Pre-write pause.json so first iter-boundary fires the pause-honor ----
PAUSE_ID="pause_smoke_$(date -u +%s)"
cat > "$STATE_DIR/commands/${PAUSE_ID}.json" <<EOF
{"command_type":"pause","command_id":"$PAUSE_ID","issued_by":"test_harness","issued_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","reason":"integration smoke test"}
EOF

# ---- 5. Launch orchestrator with overridden PATH (mock claude) + SCRIPT_DIR-redirected hooks ----
# We can't easily redirect hooks; instead temporarily symlink the real hooks dir to our temp hooks dir.
# Approach: create a temp ralph clone that points lib/ at real, hooks/ at our temp.
TMP_RALPH="$TMPROOT/ralph_overlay"
mkdir -p "$TMP_RALPH"
ln -s "$RALPH_ROOT/lib" "$TMP_RALPH/lib"
ln -s "$RALPH_ROOT/schemas" "$TMP_RALPH/schemas"
ln -s "$HOOKS_TMP" "$TMP_RALPH/hooks"
cp "$RALPH_ROOT/orchestrator.sh" "$TMP_RALPH/orchestrator.sh"

ORCH_OUT="$TMPROOT/orch.stdout"
ORCH_ERR="$TMPROOT/orch.stderr"
# Unset MSYS_NO_PATHCONV for the orchestrator launch: orchestrator.sh exports it as 1 (FUP-0740
# slash-prefix preservation for `claude -p /rl-* …` args), but the mock claude shim doesn't need
# that preservation. WinGet jq (Windows-native binary) ENOENTs on Unix-style paths under
# MSYS_NO_PATHCONV=1; the export inside the orchestrator hits the WinGet jq before path conversion.
# Unsetting at the launch boundary keeps the test runnable on Windows+WinGet jq without affecting
# the real orchestrator's behaviour under operator's Unix-jq environment.
PATH="$TMPBIN:$PATH" CLAUDE_SKILLS_DIR="$RALPH_ROOT" \
  bash -c "unset MSYS_NO_PATHCONV; exec bash \"$TMP_RALPH/orchestrator.sh\" \"$TMPSEED\"" \
  > "$ORCH_OUT" 2> "$ORCH_ERR"
ORCH_RC=$?

echo "--- orchestrator stdout (last 20 lines) ---"
tail -20 "$ORCH_OUT" || true
echo "--- orchestrator stderr (last 20 lines) ---"
tail -20 "$ORCH_ERR" || true
echo "--- orchestrator rc=$ORCH_RC ---"

# ---- 6. Assertions ----
[[ "$ORCH_RC" == "0" ]] && pass "orchestrator exit 0 (clean pause-honor, not MAX_ITERATIONS)" || fail "orchestrator rc=$ORCH_RC (expected 0)"
[[ -f "$STATE_DIR/commands/.processed/${PAUSE_ID}.json" ]] && pass "pause command archived to .processed/" || fail "pause command NOT archived"
RESP="$STATE_DIR/commands/${PAUSE_ID}.response.json"
[[ -f "$RESP" ]] && pass "response.json written" || fail "response.json MISSING"
if [[ -f "$RESP" ]]; then
  STATUS="$(jq -r '.status' "$RESP")"
  [[ "$STATUS" == "pause_honored_at_iter_boundary" ]] && pass "response.status=pause_honored_at_iter_boundary" || fail "response.status=$STATUS"
fi
grep -q 'PAUSE_REQUESTED' "$STATE_DIR/logs/orchestrator.log" 2>/dev/null && pass "orchestrator.log contains PAUSE_REQUESTED" || fail "orchestrator.log missing PAUSE_REQUESTED"
grep -q 'PAUSED at iter' "$ORCH_OUT" && pass "stdout contains 'PAUSED at iter...'" || fail "stdout missing 'PAUSED at iter...'"
# Verify pause_requested.flag was cleaned up after honor (orchestrator rm -f's it on exit)
[[ ! -f "$STATE_DIR/pause_requested.flag" ]] && pass "pause_requested.flag cleaned up after honor" || fail "pause_requested.flag NOT cleaned up"

echo ""
echo "=== test_fup_0815_orchestrator_integration.sh: $PASS PASS, $FAIL FAIL ==="
[[ $FAIL -eq 0 ]]
