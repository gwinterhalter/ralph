#!/usr/bin/env bash
# tests/test_command_dispatch_bump_budget.sh — FUP-0815 bump_budget command dispatch test.
#
# Asserts:
#   1. command_dispatch writes $STATE_DIR/budget_override.json with .budget_cap_usd = new_cap_usd
#   2. response.json has status=budget_override_written and details.new_cap_usd matches
#   3. Command archived to .processed/
#   4. Negative case: schema validation fails when new_cap_usd is missing
#
# Run: bash tests/test_command_dispatch_bump_budget.sh
# Exit 0 = all PASS; exit 1 = any FAIL.

set -uo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RALPH_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

PASS_COUNT=0
FAIL_COUNT=0
fail() { echo "FAIL: $*" >&2; FAIL_COUNT=$((FAIL_COUNT+1)); }
pass() { echo "PASS: $*"; PASS_COUNT=$((PASS_COUNT+1)); }

make_seed() {
  cat > "$1" <<'EOF'
---
seed_schema_version: 1.3
workspace_root: "/tmp"
state_dir_relative: "state/"
work_registry: "registry.md"
budget:
  tokens_usd: 1.00
---
body irrelevant
EOF
}

# shellcheck source=../lib/command_dispatch.sh
source "$RALPH_ROOT/lib/command_dispatch.sh"

# -------- Case 1: bump_budget honored --------
STATE_DIR="$TMPROOT/case1_state"
mkdir -p "$STATE_DIR/commands"
SEED="$TMPROOT/case1_seed.md"
make_seed "$SEED"
CMD_ID="bump_test_$(date -u +%s)"
CMD_FILE="$STATE_DIR/commands/${CMD_ID}.json"
cat > "$CMD_FILE" <<EOF
{"command_type":"bump_budget","command_id":"$CMD_ID","issued_by":"test","issued_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","new_cap_usd":25.50}
EOF

command_dispatch_run "$STATE_DIR" "$SEED" "0001"

RESP_FILE="$STATE_DIR/commands/${CMD_ID}.response.json"
[[ -f "$RESP_FILE" ]] && pass "case1: response.json written" || fail "case1: response.json MISSING"
STATUS="$(jq -r '.status' "$RESP_FILE" 2>/dev/null || echo MISSING)"
[[ "$STATUS" == "budget_override_written" ]] && pass "case1: status=budget_override_written" || fail "case1: status=$STATUS"
[[ -f "$STATE_DIR/budget_override.json" ]] && pass "case1: budget_override.json written" || fail "case1: budget_override.json MISSING"
ACTUAL_CAP="$(jq -r '.budget_cap_usd' "$STATE_DIR/budget_override.json" 2>/dev/null || echo MISSING)"
[[ "$ACTUAL_CAP" == "25.50" ]] && pass "case1: budget_override.json.budget_cap_usd=25.50" || fail "case1: budget_override.json.budget_cap_usd=$ACTUAL_CAP (expected 25.50)"
RESP_CAP="$(jq -r '.details.new_cap_usd' "$RESP_FILE" 2>/dev/null || echo MISSING)"
[[ "$RESP_CAP" == "25.5" || "$RESP_CAP" == "25.50" ]] && pass "case1: response details.new_cap_usd matches" || fail "case1: response details.new_cap_usd=$RESP_CAP"
[[ -f "$STATE_DIR/commands/.processed/${CMD_ID}.json" ]] && pass "case1: command archived" || fail "case1: archive MISSING"

# -------- Case 2: schema validation fails on missing new_cap_usd --------
STATE_DIR="$TMPROOT/case2_state"
mkdir -p "$STATE_DIR/commands"
SEED="$TMPROOT/case2_seed.md"
make_seed "$SEED"
CMD_ID="bump_test_$(date -u +%s)_case2"
CMD_FILE="$STATE_DIR/commands/${CMD_ID}.json"
cat > "$CMD_FILE" <<EOF
{"command_type":"bump_budget","command_id":"$CMD_ID","issued_by":"test","issued_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF

command_dispatch_run "$STATE_DIR" "$SEED" "0001"

RESP_FILE="$STATE_DIR/commands/${CMD_ID}.response.json"
[[ -f "$RESP_FILE" ]] && pass "case2: response.json written (validation fail surface)" || fail "case2: response.json MISSING"
STATUS="$(jq -r '.status' "$RESP_FILE" 2>/dev/null || echo MISSING)"
[[ "$STATUS" == "schema_validation_failed" ]] && pass "case2: status=schema_validation_failed (missing required field)" || fail "case2: status=$STATUS (expected schema_validation_failed)"
[[ ! -f "$STATE_DIR/budget_override.json" ]] && pass "case2: budget_override.json NOT written (validation blocked write)" || fail "case2: budget_override.json WRITTEN despite validation fail"

echo ""
echo "=== test_command_dispatch_bump_budget.sh: $PASS_COUNT PASS, $FAIL_COUNT FAIL ==="
[[ $FAIL_COUNT -eq 0 ]]
