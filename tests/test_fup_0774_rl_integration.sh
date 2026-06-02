#!/usr/bin/env bash
# tests/test_fup_0774_rl_integration.sh — FUP-0774 RL integration test.
#
# Spins up a minimal Ralph Loop substrate (seed + state_dir + ITER_DIR + gate_request) and
# runs the actual hooks/execute_with_gates.sh broker end-to-end. The `claude` binary is
# replaced via a PATH shim that mocks the Answerer to emit a MALFORMED gate_response —
# triggering the demote-detection block in execute_with_gates.sh (lines 183-202). Asserts:
#
#   1. execute_with_gates.sh exits non-zero (escalation; expected 1)
#   2. stderr contains "Answerer demoted gate_dc <id> to gate_human" (existing surface)
#   3. notifications.log contains an entry with reason="answerer_demote"
#      (FUP-0774 fix — this signal is now JSON-queryable, not just stderr-grep-able)
#   4. state_snapshot.json gets pending_gate set (the resume marker)
#   5. The demote notification is the ONLY answerer_demote-reason entry (no duplicate fires)
#   6. The notifications.log entry preserves the full schema
#      (event/iteration/gate_id/reason/channel_attempted/channel_result/ts)
#
# Unlike test_fup_0774_notification_reason.sh (which tests dispatch_notification in
# isolation), this test exercises the REAL hooks/execute_with_gates.sh code path including
# the gate_request validation, broker classification, Answerer invocation (mocked), demote
# detection, and audit append. It is the "RL integration" complement to the hermetic unit
# test.
#
# Cost: zero. The mock `claude` shim emits canned responses; no real API call.
#
# Run: bash tests/test_fup_0774_rl_integration.sh
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

mkdir -p "$TMPBIN" "$ITER_DIR" "$STATE_DIR/logs"

# ---- Mock `claude` binary ----
# Detect call shape via positional args:
#   Answerer call:  contains "/rl-operator-answerer <path-to-gate_request>"
#   Executor call:  invoked with `--print --output-format json` and reads stdin from a session_plan
#
# For the Answerer call, write a MALFORMED gate_response (missing the required `reasoning` field
# per FR-008; the validation at execute_with_gates.sh:193 demands reasoning length > 0). This
# triggers `demoted=1` at line 194, causing the demote-notification dispatch at line 199.
cat > "$TMPBIN/claude" <<'CLAUDE_SHIM'
#!/usr/bin/env bash
# Mock claude — emits canned outputs based on call shape.
# Stderr-log every invocation to a sibling file so the test can audit.
echo "MOCK_CLAUDE_CALL: $*" >> "$TMPSHIM_LOG"

# Detect Answerer call by /rl-operator-answerer arg
for arg in "$@"; do
  if [[ "$arg" == */rl-operator-answerer* ]]; then
    is_answerer=1
    # Extract gate_request path (the arg immediately after /rl-operator-answerer in the slash command)
    # The shape is: /rl-operator-answerer <path-to-gate_request.json>
    gate_req_path="${arg#*/rl-operator-answerer }"
    break
  fi
done

if [[ "${is_answerer:-0}" == "1" ]]; then
  # Write a gate_escalation_*.md artefact to trigger the demote-detection path (the OTHER
  # branch at execute_with_gates.sh:189). Critically, do NOT write a gate_response.json so
  # Pass 2 sees no response file, escalates the request to operator-wait, and writes the
  # pending_gate marker to state_snapshot.json.
  #
  # We picked this branch over the "malformed gate_response" path because the latter leaves
  # the response file on disk; Pass 2 checks only file existence (not response validity) and
  # treats the file as "operator-resolved, proceeding" — bypassing the escalation + pending_gate
  # write. Per §5.3, the Answerer can self-escalate by emitting a gate_escalation artefact
  # alongside an absent / minimal gate_response, and that's what this mock simulates.
  req_suffix="$(basename "$gate_req_path" .json)"
  req_suffix="${req_suffix#gate_request_}"
  iter_dir="$(dirname "$gate_req_path")"
  cat > "$iter_dir/gate_escalation_${req_suffix}.md" <<MARKDOWN
# Mock Answerer Self-Escalation (FUP-0774 RL Integration Test)

**gate_id:** G-test-demote

**Reason for escalation:** confidence below threshold; question requires irreversible / out-of-scope decision per §5.3.

This artefact triggers the demote-detection branch at execute_with_gates.sh:189
(\`-n "\$demote_artefact"\`) and causes the broker to dispatch a notification with
reason="answerer_demote" — the FUP-0774 fix surface under test.
MARKDOWN
  echo "Mock Answerer: emitted gate_escalation_${req_suffix}.md to trigger demote (no gate_response written)"
  exit 0
fi

# Executor call (--print --output-format json reading session_plan from stdin).
# Emit a valid execution_result.json envelope so validate_artefact.sh doesn't reject it.
# Note: in this test, the script should exit at the gate broker BEFORE reaching the
# Executor invocation (any_blocked==1 path), so this branch shouldn't fire.
jq -nc '{
  type:"result",
  subtype:"success",
  result:"Mock Executor: test fixture; should not have been reached",
  total_cost_usd: 0,
  num_turns: 0
}'
CLAUDE_SHIM
chmod +x "$TMPBIN/claude"

# ---- Mock `validate_artefact.sh` ----
# The real one uses Python jsonschema which may not be set up. For this test, bypass with a
# permissive validator (just JSON parse). Place it in our PATH; since execute_with_gates.sh
# calls it with a relative path (`$SCRIPT_DIR/../lib/validate_artefact.sh`), we can't shim
# via PATH. Instead, we accept whatever validate_artefact.sh does. If it's strict, the test
# will fail loudly with a parse error; verifying the fixture stays schema-valid is part of
# the substrate construction.

# ---- Seed ----
cat > "$TMPSEED" <<'SEED_EOF'
---
seed_schema_version: 1.4
initiative:
  slug: fup-0774-rl-test
  title: FUP-0774 RL integration test
  owner: test
workspace_root: /tmp/fup-0774-rl-test
read_only_paths: []
mcp_servers: []
state_dir_relative: state
work_registry: /tmp/fup-0774-rl-test/registry.md
context_documents: []
session_shape_catalog: []
verification_bindings: {}
completion_predicate: []
gate_policy:
  pre_classification: []
  confidence_threshold: 0.7
budget:
  iterations_max: 1
  per_call_usd_cap: 1.00
  tokens_usd: 1.00
  max_turns_per_call: 5
  hang_timeout_seconds: 60
notification_channel:
  primary: gmail_smtp
  primary_smtp_user: nobody@example.com
  primary_to_address: nobody@example.com
  primary_smtp_host: smtp.example.com
  primary_smtp_port: 587
  primary_env_vars:
    smtp_app_password: FUP_0774_RL_TEST_NONEXISTENT
  fallback: ""
permission_posture: "--permission-mode auto"
---
SEED_EOF

# ---- Iteration substrate ----
# session_plan_0001.md — minimal; execute_with_gates.sh should exit at gate broker before
# reading it.
cat > "$ITER_DIR/session_plan_$ITER.md" <<'PLAN_EOF'
# Mock Session Plan for FUP-0774 RL integration test
This plan is a no-op stub; the test exits at the gate broker before the Executor reads it.
PLAN_EOF

# gate_request_0001_0001.json — a gate_dc default (no cluster, no pre_classification match)
cat > "$ITER_DIR/gate_request_${ITER}_0001.json" <<'GR_EOF'
{
  "gate_id": "G-test-demote",
  "question_text": "Mock gate_request for FUP-0774 demote test. Should the test verify the notification fires?",
  "options": ["A", "B"],
  "cluster": null,
  "iteration": 1
}
GR_EOF

# ---- Ensure env vars unset (so channel resolves to skipped:env_unset; no real email) ----
unset FUP_0774_RL_TEST_NONEXISTENT

# ---- Setup mock claude log file (shim writes here) ----
export TMPSHIM_LOG="$TMPROOT/mock_claude_calls.log"
: > "$TMPSHIM_LOG"

# ---- Run execute_with_gates.sh with the mock binary in PATH ----
export PATH="$TMPBIN:$PATH"
# Also export CLAUDE_SKILLS_DIR (referenced by execute_with_gates.sh:128 Answerer call)
export CLAUDE_SKILLS_DIR="/tmp/nonexistent-skills"

stderr_file="$TMPROOT/exec.stderr"
stdout_file="$TMPROOT/exec.stdout"
set +e
bash "$RALPH_ROOT/hooks/execute_with_gates.sh" "$TMPSEED" "$ITER_DIR" \
  > "$stdout_file" 2> "$stderr_file"
exec_rc=$?
set -e

echo "=== execute_with_gates.sh ran with rc=$exec_rc ==="
echo "=== stderr (last 30 lines) ==="
tail -30 "$stderr_file" 2>/dev/null || echo "(no stderr)"
echo ""

# ---- Check assertions ----
pass=0
fail=0
failed_checks=()

check() {
  local desc="$1"; local expected="$2"; local actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    pass=$((pass+1))
    echo "  PASS — $desc"
  else
    fail=$((fail+1))
    failed_checks+=("$desc")
    echo "  FAIL — $desc" >&2
    echo "    expected: $expected" >&2
    echo "    actual:   $actual" >&2
  fi
}

NOTIF_LOG="$STATE_DIR/logs/notifications.log"

echo ""
echo "Check 1 — execute_with_gates.sh exits 1 (gate_human escalation; demote → defer → escalate)"
check "exit code = 1" "1" "$exec_rc"

echo ""
echo "Check 2 — stderr contains the demote signal (existing surface preserved)"
check "stderr contains 'Answerer demoted gate_dc'" \
      "true" \
      "$(grep -q 'Answerer demoted gate_dc' "$stderr_file" && echo true || echo false)"

echo ""
echo "Check 3 — notifications.log exists and contains a row with reason='answerer_demote' (FUP-0774 fix)"
check "notifications.log file exists" \
      "true" \
      "$([[ -f "$NOTIF_LOG" ]] && echo true || echo false)"
if [[ -f "$NOTIF_LOG" ]]; then
  demote_rows="$(jq -sc '[.[] | select(.reason == "answerer_demote")]' "$NOTIF_LOG" 2>/dev/null)"
  demote_count="$(printf '%s' "$demote_rows" | jq -r 'length')"
  check "notifications.log has exactly 1 row with reason=answerer_demote" \
        "1" \
        "$demote_count"
  # Inspect the row
  demote_row="$(printf '%s' "$demote_rows" | jq -c '.[0]')"
  echo "    demote row: $demote_row"
  check "demote row .event == 'gate_human'" \
        "gate_human" \
        "$(printf '%s' "$demote_row" | jq -r '.event')"
  check "demote row .gate_id == 'G-test-demote'" \
        "G-test-demote" \
        "$(printf '%s' "$demote_row" | jq -r '.gate_id')"
  check "demote row .iteration == '$ITER'" \
        "$ITER" \
        "$(printf '%s' "$demote_row" | jq -r '.iteration')"
fi

echo ""
echo "Check 4 — state_snapshot.json has pending_gate set (the §6.3 resume marker)"
if [[ -f "$STATE_DIR/state_snapshot.json" ]]; then
  pending_gate_iter="$(jq -r '.pending_gate.iteration // ""' "$STATE_DIR/state_snapshot.json")"
  check "pending_gate.iteration == '$ITER'" \
        "$ITER" \
        "$pending_gate_iter"
else
  fail=$((fail+1))
  failed_checks+=("state_snapshot.json missing")
  echo "  FAIL — state_snapshot.json was not created" >&2
fi

echo ""
echo "Check 5 — notifications.log entry has the full 7-field schema"
if [[ -f "$NOTIF_LOG" ]]; then
  first_row="$(head -1 "$NOTIF_LOG")"
  expected_keys='channel_attempted,channel_result,event,gate_id,iteration,reason,ts'
  actual_keys="$(printf '%s' "$first_row" | jq -r 'keys | join(",")')"
  check "notifications.log row has 7 keys (alphabetised)" \
        "$expected_keys" \
        "$actual_keys"
fi

echo ""
echo "Check 6 — mock claude was invoked at least once (sanity)"
mock_call_count="$(wc -l < "$TMPSHIM_LOG" | tr -d ' ')"
check "mock claude shim invoked >= 1 time" \
      "true" \
      "$([[ "$mock_call_count" -ge 1 ]] && echo true || echo false)"
echo "    mock claude was invoked $mock_call_count time(s)"

# ---- Summary ----
echo ""
echo "================================================"
echo "FUP-0774 RL integration test: $pass PASS, $fail FAIL"
echo "================================================"
if (( fail > 0 )); then
  echo ""
  echo "FAILED checks:" >&2
  for c in "${failed_checks[@]}"; do echo "  - $c" >&2; done
  echo ""
  echo "=== Full notifications.log content ===" >&2
  if [[ -f "$NOTIF_LOG" ]]; then jq . "$NOTIF_LOG" >&2 2>/dev/null || cat "$NOTIF_LOG" >&2; fi
  echo ""
  echo "=== Full stderr ===" >&2
  cat "$stderr_file" >&2
  echo ""
  echo "=== Mock claude calls ===" >&2
  cat "$TMPSHIM_LOG" >&2
  exit 1
fi

exit 0
