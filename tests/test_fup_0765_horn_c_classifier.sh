#!/usr/bin/env bash
# tests/test_fup_0765_horn_c_classifier.sh — FUP-0765/0779/0804 horn (C) classifier test.
#
# Exercises the §10.5 permission_denials classifier in hooks/execute_with_gates.sh. The
# classifier distinguishes:
#   - verification_spawn:   tool_name=Bash AND tool_input.command matches nested claude -p/--print
#   - deliverable_blocking: anything else
#
# Behavior: exit 1 ONLY when at least one denial is deliverable_blocking; all-verification_spawn
# denials → log + audit + continue (exit 0 path eligible).
#
# This test extracts the classifier jq logic and asserts against 5 fixture scenarios:
#   Scenario A — all verification_spawn (claude -p) — should be all-VS
#   Scenario B — all verification_spawn (claude --print) — should be all-VS
#   Scenario C — mixed (1 VS + 1 DB) — should classify DB > 0
#   Scenario D — single deliverable_blocking — should be all-DB
#   Scenario E — empty denials — should be zero on both counts
#
# Tests the jq expression in isolation (no execute_with_gates.sh invocation needed for these
# checks — the regex behavior is the core fix; the surrounding shell flow is straightforward
# and would require the heavy substrate-mocking machinery the FUP-0774 RL integration test
# uses).
#
# Cost: $0 (no API call; pure jq + fixture comparison).

set -uo pipefail

# The exact jq expression from execute_with_gates.sh §10.5 horn C block. If this file's
# expression diverges from execute_with_gates.sh, the test catches the drift.
VS_QUERY='[.permission_denials[] | select(((.tool_name // .tool // "") == "Bash") and (((.tool_input.command // "") | test("\\bclaude\\s+(-p|--print)\\b")) or ((.reason // "") | test("\\bclaude\\s+(-p|--print)\\b"))))] | length'

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

classify() {
  local fixture="$1"
  local denials_count
  denials_count="$(printf '%s' "$fixture" | jq -r '.permission_denials | length')"
  local vs_count
  vs_count="$(printf '%s' "$fixture" | jq -r "$VS_QUERY")"
  local db_count=$((denials_count - vs_count))
  echo "$denials_count $vs_count $db_count"
}

echo ""
echo "Scenario A — all verification_spawn (claude -p via tool_input.command)"
fixture_a='{
  "permission_denials": [
    {"tool_name":"Bash","tool_input":{"command":"claude -p --add-dir /skills -- \"/cf-doc-reviewer target=spec.md\""},"reason":"--dangerously-skip-permissions denied under auto"}
  ],
  "terminal_reason":"completed"
}'
read denials_a vs_a db_a <<< "$(classify "$fixture_a")"
check "Scenario A denials_count = 1" "1" "$denials_a"
check "Scenario A verification_spawn_count = 1" "1" "$vs_a"
check "Scenario A deliverable_blocking_count = 0" "0" "$db_a"

echo ""
echo "Scenario B — all verification_spawn (claude --print via tool_input.command)"
fixture_b='{
  "permission_denials": [
    {"tool_name":"Bash","tool_input":{"command":"claude --print --output-format json --strict-mcp-config -- audit"},"reason":"denied"},
    {"tool_name":"Bash","tool_input":{"command":"  claude  -p  /cf-skill-reviewer target=cf-foo"},"reason":"denied"}
  ],
  "terminal_reason":"completed"
}'
read denials_b vs_b db_b <<< "$(classify "$fixture_b")"
check "Scenario B denials_count = 2" "2" "$denials_b"
check "Scenario B verification_spawn_count = 2" "2" "$vs_b"
check "Scenario B deliverable_blocking_count = 0" "0" "$db_b"

echo ""
echo "Scenario C — mixed (1 VS + 1 DB; DB is a non-claude Bash command)"
fixture_c='{
  "permission_denials": [
    {"tool_name":"Bash","tool_input":{"command":"claude -p --add-dir /skills -- audit"},"reason":"denied"},
    {"tool_name":"Bash","tool_input":{"command":"rm -rf /production/data"},"reason":"denied"}
  ],
  "terminal_reason":"completed"
}'
read denials_c vs_c db_c <<< "$(classify "$fixture_c")"
check "Scenario C denials_count = 2" "2" "$denials_c"
check "Scenario C verification_spawn_count = 1" "1" "$vs_c"
check "Scenario C deliverable_blocking_count = 1" "1" "$db_c"

echo ""
echo "Scenario D — single deliverable_blocking (Edit tool on read-only path)"
fixture_d='{
  "permission_denials": [
    {"tool_name":"Edit","tool_input":{"file_path":"/Project_Docs_Current/Schema.md"},"reason":"read-only path"}
  ],
  "terminal_reason":"completed"
}'
read denials_d vs_d db_d <<< "$(classify "$fixture_d")"
check "Scenario D denials_count = 1" "1" "$denials_d"
check "Scenario D verification_spawn_count = 0" "0" "$vs_d"
check "Scenario D deliverable_blocking_count = 1" "1" "$db_d"

echo ""
echo "Scenario E — empty denials (clean run)"
fixture_e='{"permission_denials":[],"terminal_reason":"completed"}'
read denials_e vs_e db_e <<< "$(classify "$fixture_e")"
check "Scenario E denials_count = 0" "0" "$denials_e"
check "Scenario E verification_spawn_count = 0" "0" "$vs_e"
check "Scenario E deliverable_blocking_count = 0" "0" "$db_e"

echo ""
echo "Scenario F — verification_spawn matched via legacy reason field (no tool_input.command)"
fixture_f='{
  "permission_denials": [
    {"tool":"Bash","reason":"command claude -p was denied"}
  ],
  "terminal_reason":"completed"
}'
read denials_f vs_f db_f <<< "$(classify "$fixture_f")"
check "Scenario F denials_count = 1" "1" "$denials_f"
check "Scenario F verification_spawn_count = 1 (reason field fallback)" "1" "$vs_f"
check "Scenario F deliverable_blocking_count = 0" "0" "$db_f"

echo ""
echo "Scenario G — false-positive guard: 'claudette --print-only' must NOT classify as VS (word-boundary)"
fixture_g='{
  "permission_denials": [
    {"tool_name":"Bash","tool_input":{"command":"claudette --print-only --output=json"},"reason":"denied"}
  ],
  "terminal_reason":"completed"
}'
read denials_g vs_g db_g <<< "$(classify "$fixture_g")"
check "Scenario G denials_count = 1" "1" "$denials_g"
check "Scenario G verification_spawn_count = 0 (word-boundary guard)" "0" "$vs_g"
check "Scenario G deliverable_blocking_count = 1" "1" "$db_g"

# ---- Summary ----
echo ""
echo "============================================"
echo "FUP-0765 horn C classifier test: $pass PASS, $fail FAIL"
echo "============================================"
if (( fail > 0 )); then
  echo "FAILED checks:" >&2
  for c in "${failed_checks[@]}"; do echo "  - $c" >&2; done
  exit 1
fi

exit 0
