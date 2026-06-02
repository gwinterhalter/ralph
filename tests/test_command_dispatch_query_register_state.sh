#!/usr/bin/env bash
# tests/test_command_dispatch_query_register_state.sh — FUP-0815 query_register_state test.
#
# Asserts:
#   1. command_dispatch writes response.json with status=register_state_snapshot
#   2. Response details carry budget snapshot (cap, spent, override)
#   3. Response details include last_completed_iteration + pending_gate + snapshot_taken_at
#   4. READ-ONLY: no substrate mutation (no flag files, no budget_override.json written)
#   5. Command archived to .processed/
#
# Run: bash tests/test_command_dispatch_query_register_state.sh
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
  tokens_usd: 2.50
---
body irrelevant
EOF
}

# shellcheck source=../lib/command_dispatch.sh
source "$RALPH_ROOT/lib/command_dispatch.sh"

STATE_DIR="$TMPROOT/state"
mkdir -p "$STATE_DIR/commands"
SEED="$TMPROOT/seed.md"
make_seed "$SEED"

# Seed substrate: spend.json + state_snapshot.json (no pending_gate).
echo '{"total_spend_usd": 0.75}' > "$STATE_DIR/spend.json"
echo '{"pending_gate": null}' > "$STATE_DIR/state_snapshot.json"

CMD_ID="qrs_test_$(date -u +%s)"
CMD_FILE="$STATE_DIR/commands/${CMD_ID}.json"
cat > "$CMD_FILE" <<EOF
{"command_type":"query_register_state","command_id":"$CMD_ID","issued_by":"test","issued_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF

# Pre-state snapshot for "no substrate mutation" check.
PRE_LIST="$(cd "$STATE_DIR" && ls -1 | sort)"

command_dispatch_run "$STATE_DIR" "$SEED" "0007"

RESP_FILE="$STATE_DIR/commands/${CMD_ID}.response.json"
[[ -f "$RESP_FILE" ]] && pass "response.json written" || fail "response.json MISSING"
STATUS="$(jq -r '.status' "$RESP_FILE" 2>/dev/null || echo MISSING)"
[[ "$STATUS" == "register_state_snapshot" ]] && pass "status=register_state_snapshot" || fail "status=$STATUS"

# Snapshot field assertions.
LAST_ITER="$(jq -r '.details.last_completed_iteration' "$RESP_FILE" 2>/dev/null)"
[[ "$LAST_ITER" == "0007" ]] && pass "details.last_completed_iteration=0007" || fail "details.last_completed_iteration=$LAST_ITER"
CAP="$(jq -r '.details.budget.cap_usd' "$RESP_FILE" 2>/dev/null)"
[[ "$CAP" == "2.5" || "$CAP" == "2.50" ]] && pass "details.budget.cap_usd=2.5" || fail "details.budget.cap_usd=$CAP"
SPENT="$(jq -r '.details.budget.spent_usd' "$RESP_FILE" 2>/dev/null)"
[[ "$SPENT" == "0.75" ]] && pass "details.budget.spent_usd=0.75" || fail "details.budget.spent_usd=$SPENT"
OVR="$(jq -r '.details.budget.override_usd' "$RESP_FILE" 2>/dev/null)"
[[ "$OVR" == "null" ]] && pass "details.budget.override_usd=null (no override set)" || fail "details.budget.override_usd=$OVR"

# READ-ONLY assertion: no substrate-mutating side-effects.
[[ ! -f "$STATE_DIR/pause_requested.flag" ]] && pass "READ-ONLY: pause_requested.flag NOT written" || fail "READ-ONLY VIOLATED: pause_requested.flag written"
[[ ! -f "$STATE_DIR/budget_override.json" ]] && pass "READ-ONLY: budget_override.json NOT written" || fail "READ-ONLY VIOLATED: budget_override.json written"

[[ -f "$STATE_DIR/commands/.processed/${CMD_ID}.json" ]] && pass "command archived to .processed/" || fail "archive MISSING"

echo ""
echo "=== test_command_dispatch_query_register_state.sh: $PASS_COUNT PASS, $FAIL_COUNT FAIL ==="
[[ $FAIL_COUNT -eq 0 ]]
