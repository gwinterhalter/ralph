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

# FUP-0823 closure: orchestrator.sh run_claude_json now wraps jq file arguments with
# `cygpath -w` when cygpath is available (mirroring lib/validate_artefact.sh L30-36
# pattern). This makes the test runnable under Windows+WinGet jq.exe; passes through
# unchanged on Unix-jq hosts.

PASS=0; FAIL=0
fail() { echo "FAIL: $*" >&2; FAIL=$((FAIL+1)); }
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }

# ---- 1. Mock claude shim (canned JSON for Planner + Consumer) ----
mkdir -p "$TMPBIN"
cat > "$TMPBIN/claude" <<'SHIM'
#!/usr/bin/env bash
# Mock claude for orchestrator integration test. Three role-call shapes:
#   1. `claude --print --output-format json ... < session_plan` — Executor (from execute_with_gates.sh
#      L271; reads session_plan from stdin, emits execution_result.schema-conformant JSON to stdout)
#   2. `claude -p ... -- "/rl-initiative-planner $STATE_DIR $ITER_DIR"` — Planner (from orchestrator.sh
#      run_claude_json call site; prompt is the LAST arg as a single quoted string)
#   3. `claude -p ... -- "/rl-iteration-consumer $STATE_DIR $ITER_DIR"` — Consumer (same call shape)
#   4. `claude -p ... -- "/rl-operator-answerer $req"` — Answerer (from execute_with_gates.sh L169;
#      handled defensively even though no gate_dc is expected in the smoke-test sandbox)
PROMPT="$*"

# Detect --print mode (Executor): stdin → session plan; stdout → execution_result JSON.
if echo "$PROMPT" | grep -qw -- '--print'; then
  # The mock doesn't need to actually parse stdin — execute_with_gates.sh redirects our stdout to
  # $ITER_DIR/.execution_result_${ITER}.cli.json. Emit a schema-conformant payload:
  #   required: session_id, result, total_cost_usd, permission_denials, terminal_reason='completed'
  cat > /dev/null  # drain stdin (the session plan); we don't need it
  cat <<EOF
{
  "session_id": "mock-executor-session-fup-0815-smoke",
  "type": "result",
  "subtype": "success",
  "result": "Mock Executor output for FUP-0815 integration smoke test. No verification spawns; no permission denials; terminal_reason=completed so execute_with_gates §10.5 FR-019 post-execution gate PASSes and orchestrator proceeds to Consumer + iter-boundary command_dispatch hook.",
  "total_cost_usd": 0.0,
  "permission_denials": [],
  "terminal_reason": "completed",
  "duration_ms": 1,
  "duration_api_ms": 0,
  "num_turns": 0,
  "is_error": false,
  "usage": {"input_tokens": 0, "output_tokens": 0}
}
EOF
  exit 0
fi

# -p mode: extract ITER_DIR from the LAST space-separated token of the LAST arg.
LAST_ARG="${!#}"
ITER_DIR="${LAST_ARG##* }"
[[ "$ITER_DIR" != */iterations/* ]] && ITER_DIR="."
mkdir -p "$ITER_DIR" 2>/dev/null || true
ITER_NUM="$(basename "$ITER_DIR")"

if echo "$PROMPT" | grep -q '/rl-initiative-planner'; then
  # Planner: write minimal session_plan + emit JSON envelope for orchestrator.run_claude_json
  cat > "$ITER_DIR/session_plan_${ITER_NUM}.md" <<EOF
---
iteration_index: ${ITER_NUM}
shape: noop
max_turns: 1
target_item_id: noop_target
---
# Mock session plan (test_fup_0815 integration smoke test)
Findings: 0 BLOCKER, 0 DRIFT
EOF
  echo '{"result":"mock planner output (no INITIATIVE_COMPLETE)","total_cost_usd":0.0}'
elif echo "$PROMPT" | grep -q '/rl-iteration-consumer'; then
  # Consumer: write minimal state_snapshot.json so post-iter command_dispatch can read pending_gate;
  # also clear pending_gate explicitly so pause-honor isn't deferred per FUP-0797 BINDING.
  STATE_DIR="${ITER_DIR%/iterations/*}"
  if [[ -d "$STATE_DIR" ]]; then
    if [[ -f "$STATE_DIR/state_snapshot.json" ]]; then
      jq '.pending_gate = null' "$STATE_DIR/state_snapshot.json" > "$STATE_DIR/state_snapshot.json.tmp" \
        && mv "$STATE_DIR/state_snapshot.json.tmp" "$STATE_DIR/state_snapshot.json"
    else
      echo '{"pending_gate": null}' > "$STATE_DIR/state_snapshot.json"
    fi
  fi
  echo '{"result":"mock consumer output","total_cost_usd":0.0}'
elif echo "$PROMPT" | grep -q '/rl-operator-answerer'; then
  # Answerer (defensive — smoke test should not trigger gate_dc, but mock anyway): write a
  # schema-conformant gate_response with selected_option for any inferred gate_id.
  echo '{"result":"mock answerer output","total_cost_usd":0.0}'
else
  # Unknown -p call shape — emit a schema-conformant execution_result anyway so we don't break
  # any downstream validator.
  cat <<EOF
{"session_id":"mock-unknown","result":"mock unknown","total_cost_usd":0.0,"permission_denials":[],"terminal_reason":"completed"}
EOF
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

# ---- 4. Pre-write all 3 \btw command-JSONs so they ALL dispatch at the first iter-boundary ----
# Order: status (read-only) → bump (writes budget_override.json) → pause (writes pause_requested.flag).
# All 3 are processed in one command_dispatch_run call at the iter-end hook (orchestrator.sh main loop
# AFTER Consumer-close + heartbeat AND BEFORE pause-honor's clean-exit). The pause-honor block sees
# pause_requested.flag last and exits 0 — but only AFTER status + bump have produced their responses.
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
STATUS_ID="query_register_state_smoke_$(date -u +%s)"
BUMP_ID="bump_budget_smoke_$(date -u +%s)"
PAUSE_ID="pause_smoke_$(date -u +%s)"
# Simulates: `\btw status`
echo "{\"command_type\":\"query_register_state\",\"command_id\":\"$STATUS_ID\",\"issued_by\":\"test_harness\",\"issued_at\":\"$TS\"}" > "$STATE_DIR/commands/${STATUS_ID}.json"
# Simulates: `\btw bump 5.00`
echo "{\"command_type\":\"bump_budget\",\"command_id\":\"$BUMP_ID\",\"issued_by\":\"test_harness\",\"issued_at\":\"$TS\",\"new_cap_usd\":5.00,\"reason\":\"integration smoke test\"}" > "$STATE_DIR/commands/${BUMP_ID}.json"
# Simulates: `\btw pause`
echo "{\"command_type\":\"pause\",\"command_id\":\"$PAUSE_ID\",\"issued_by\":\"test_harness\",\"issued_at\":\"$TS\",\"reason\":\"integration smoke test\"}" > "$STATE_DIR/commands/${PAUSE_ID}.json"

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
# FUP-0858: the sandbox seed declares no notification_channel, so dispatch_notification
# falls through to the win11toast fallback, whose un-timeboxed desktop toast() blocked the
# pause-exit path (the prior intermittent hang). RALPH_DISABLE_DESKTOP_TOAST=1 exercises the
# new headless guard so the test never touches the desktop/network — fully deterministic.
RALPH_DISABLE_DESKTOP_TOAST=1 PATH="$TMPBIN:$PATH" CLAUDE_SKILLS_DIR="$RALPH_ROOT" \
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

# --- \btw status (query_register_state) ---
STATUS_RESP="$STATE_DIR/commands/${STATUS_ID}.response.json"
[[ -f "$STATUS_RESP" ]] && pass "[status] response.json written" || fail "[status] response.json MISSING"
if [[ -f "$STATUS_RESP" ]]; then
  STATUS_STAT="$(jq -r '.status' "$STATUS_RESP")"
  [[ "$STATUS_STAT" == "register_state_snapshot" ]] && pass "[status] .status=register_state_snapshot" || fail "[status] .status=$STATUS_STAT"
  SNAP_LAST="$(jq -r '.details.last_completed_iteration' "$STATUS_RESP")"
  [[ "$SNAP_LAST" == "0001" ]] && pass "[status] details.last_completed_iteration=0001" || fail "[status] details.last_completed_iteration=$SNAP_LAST"
  SNAP_CAP="$(jq -r '.details.budget.cap_usd' "$STATUS_RESP")"
  [[ "$SNAP_CAP" == "0.1" || "$SNAP_CAP" == "0.10" ]] && pass "[status] details.budget.cap_usd matches seed 0.10" || fail "[status] details.budget.cap_usd=$SNAP_CAP"
fi
[[ -f "$STATE_DIR/commands/.processed/${STATUS_ID}.json" ]] && pass "[status] command archived to .processed/" || fail "[status] command NOT archived"

# --- \btw bump 5 (bump_budget) ---
BUMP_RESP="$STATE_DIR/commands/${BUMP_ID}.response.json"
[[ -f "$BUMP_RESP" ]] && pass "[bump] response.json written" || fail "[bump] response.json MISSING"
if [[ -f "$BUMP_RESP" ]]; then
  BUMP_STAT="$(jq -r '.status' "$BUMP_RESP")"
  [[ "$BUMP_STAT" == "budget_override_written" ]] && pass "[bump] .status=budget_override_written" || fail "[bump] .status=$BUMP_STAT"
fi
[[ -f "$STATE_DIR/budget_override.json" ]] && pass "[bump] budget_override.json written" || fail "[bump] budget_override.json MISSING"
if [[ -f "$STATE_DIR/budget_override.json" ]]; then
  OVR_CAP="$(jq -r '.budget_cap_usd' "$STATE_DIR/budget_override.json")"
  [[ "$OVR_CAP" == "5" || "$OVR_CAP" == "5.0" || "$OVR_CAP" == "5.00" ]] && pass "[bump] budget_override.json.budget_cap_usd=5" || fail "[bump] budget_override.json.budget_cap_usd=$OVR_CAP"
fi
[[ -f "$STATE_DIR/commands/.processed/${BUMP_ID}.json" ]] && pass "[bump] command archived to .processed/" || fail "[bump] command NOT archived"

# --- \btw pause (pause + honor) ---
PAUSE_RESP="$STATE_DIR/commands/${PAUSE_ID}.response.json"
[[ -f "$PAUSE_RESP" ]] && pass "[pause] response.json written" || fail "[pause] response.json MISSING"
if [[ -f "$PAUSE_RESP" ]]; then
  PAUSE_STAT="$(jq -r '.status' "$PAUSE_RESP")"
  [[ "$PAUSE_STAT" == "pause_honored_at_iter_boundary" ]] && pass "[pause] .status=pause_honored_at_iter_boundary" || fail "[pause] .status=$PAUSE_STAT"
fi
[[ -f "$STATE_DIR/commands/.processed/${PAUSE_ID}.json" ]] && pass "[pause] command archived to .processed/" || fail "[pause] command NOT archived"
grep -q 'PAUSE_REQUESTED' "$STATE_DIR/logs/orchestrator.log" 2>/dev/null && pass "[pause] orchestrator.log contains PAUSE_REQUESTED" || fail "[pause] orchestrator.log missing PAUSE_REQUESTED"
grep -q 'PAUSED at iter' "$ORCH_OUT" && pass "[pause] stdout contains 'PAUSED at iter...'" || fail "[pause] stdout missing 'PAUSED at iter...'"
[[ ! -f "$STATE_DIR/pause_requested.flag" ]] && pass "[pause] pause_requested.flag cleaned up after honor" || fail "[pause] pause_requested.flag NOT cleaned up"

echo ""
echo "=== test_fup_0815_orchestrator_integration.sh: $PASS PASS, $FAIL FAIL ==="
[[ $FAIL -eq 0 ]]
