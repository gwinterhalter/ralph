#!/usr/bin/env bash
# tests/test_command_dispatch_pause.sh — FUP-0815 pause command dispatch test.
#
# Two cases:
#   1. pause_honored: no pending_gate → command_dispatch writes pause_requested.flag,
#      writes response.json with status=pause_honored_at_iter_boundary, archives command.
#   2. pause_defers_on_pending_gate: state_snapshot.json has non-null pending_gate →
#      command_dispatch writes response.json with status=deferred_pending_gate_in_flight,
#      does NOT touch pause_requested.flag, archives command. (FUP-0797 BINDING.)
#
# Run: bash tests/test_command_dispatch_pause.sh
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

# Minimal seed (YAML frontmatter only — body irrelevant).
make_seed() {
  local f="$1"
  cat > "$f" <<'EOF'
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

# Source the unit under test.
# shellcheck source=../lib/command_dispatch.sh
source "$RALPH_ROOT/lib/command_dispatch.sh"

# -------- Case 1: pause_honored --------
STATE_DIR="$TMPROOT/case1_state"
mkdir -p "$STATE_DIR"
SEED="$TMPROOT/case1_seed.md"
make_seed "$SEED"
# state_snapshot.json absent → no pending_gate
mkdir -p "$STATE_DIR/commands"
CMD_ID="pause_test_$(date -u +%s)"
CMD_FILE="$STATE_DIR/commands/${CMD_ID}.json"
cat > "$CMD_FILE" <<EOF
{"command_type":"pause","command_id":"$CMD_ID","issued_by":"test","issued_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF

command_dispatch_run "$STATE_DIR" "$SEED" "0001"

RESP_FILE="$STATE_DIR/commands/${CMD_ID}.response.json"
[[ -f "$RESP_FILE" ]] && pass "case1: response.json written" || fail "case1: response.json MISSING"
STATUS="$(jq -r '.status' "$RESP_FILE" 2>/dev/null || echo MISSING)"
[[ "$STATUS" == "pause_honored_at_iter_boundary" ]] && pass "case1: status=pause_honored_at_iter_boundary" || fail "case1: status=$STATUS (expected pause_honored_at_iter_boundary)"
[[ -f "$STATE_DIR/pause_requested.flag" ]] && pass "case1: pause_requested.flag written" || fail "case1: pause_requested.flag MISSING"
[[ -f "$STATE_DIR/commands/.processed/${CMD_ID}.json" ]] && pass "case1: command archived to .processed/" || fail "case1: archive MISSING"
[[ ! -f "$CMD_FILE" ]] && pass "case1: original command consumed (no longer at commands/)" || fail "case1: original command STILL PRESENT at commands/"

# -------- Case 2: pause_defers_on_pending_gate (FUP-0797 BINDING) --------
STATE_DIR="$TMPROOT/case2_state"
mkdir -p "$STATE_DIR"
SEED="$TMPROOT/case2_seed.md"
make_seed "$SEED"
# state_snapshot.json with non-null pending_gate
cat > "$STATE_DIR/state_snapshot.json" <<'EOF'
{"pending_gate": {"gate_id":"G99-test-gate","iteration":"0002","raised_at":"2026-06-02T22:00:00Z"}}
EOF
mkdir -p "$STATE_DIR/commands"
CMD_ID="pause_test_$(date -u +%s)_case2"
CMD_FILE="$STATE_DIR/commands/${CMD_ID}.json"
cat > "$CMD_FILE" <<EOF
{"command_type":"pause","command_id":"$CMD_ID","issued_by":"test","issued_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF

command_dispatch_run "$STATE_DIR" "$SEED" "0002"

RESP_FILE="$STATE_DIR/commands/${CMD_ID}.response.json"
[[ -f "$RESP_FILE" ]] && pass "case2: response.json written" || fail "case2: response.json MISSING"
STATUS="$(jq -r '.status' "$RESP_FILE" 2>/dev/null || echo MISSING)"
[[ "$STATUS" == "deferred_pending_gate_in_flight" ]] && pass "case2: status=deferred_pending_gate_in_flight (FUP-0797 BINDING)" || fail "case2: status=$STATUS (expected deferred_pending_gate_in_flight)"
[[ ! -f "$STATE_DIR/pause_requested.flag" ]] && pass "case2: pause_requested.flag NOT written (pause deferred)" || fail "case2: pause_requested.flag WRITTEN despite pending_gate (FUP-0797 VIOLATION)"
[[ -f "$STATE_DIR/commands/.processed/${CMD_ID}.json" ]] && pass "case2: command archived to .processed/" || fail "case2: archive MISSING"

echo ""
echo "=== test_command_dispatch_pause.sh: $PASS_COUNT PASS, $FAIL_COUNT FAIL ==="
[[ $FAIL_COUNT -eq 0 ]]
