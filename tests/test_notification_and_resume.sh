#!/usr/bin/env bash
# tests/test_notification_and_resume.sh — static patch-presence regression guard for the
# P4-05 notification dispatch and the §6.3 step-3 / NFR-006 resume wiring.
# Zero-spend: greps the source scripts for required anchors; does not exec the orchestrator.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NOTIFY="$RALPH_DIR/lib/notify.sh"
EWG="$RALPH_DIR/hooks/execute_with_gates.sh"
ORCH="$RALPH_DIR/orchestrator.sh"

fail=0
check() {
  local label="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    echo "  PASS  $label"
  else
    echo "  FAIL  $label"
    eval "$cmd" 2>&1 | sed 's/^/    /' | head -10
    fail=1
  fi
}

echo "[1] lib/notify.sh"
check "file exists"                "[[ -f '$NOTIFY' ]]"
check "defines dispatch_notification()" "grep -q 'dispatch_notification()' '$NOTIFY'"
check "reads .notification_channel.primary"   "grep -q \"'.notification_channel.primary'\" '$NOTIFY'"
check "reads .notification_channel.fallback"  "grep -q \"'.notification_channel.fallback'\" '$NOTIFY'"
check "appends to notifications.log"          "grep -q 'notifications.log' '$NOTIFY'"

echo "[2] hooks/execute_with_gates.sh"
check "sources lib/notify.sh"      "grep -q 'source.*lib/notify.sh' '$EWG'"
check "two-pass broker (deferred_human array)" "grep -q 'deferred_human' '$EWG'"
check "gate_response precheck"     "grep -q 'gate_response_\\\$' '$EWG' || grep -q 'gate_response_\$basename_req' '$EWG' || grep -q 'gate_response_\\\$basename_req' '$EWG'"
check "pending_gate write"         "grep -q 'pending_gate' '$EWG'"
check "dispatch_notification ≥ 2" "[[ \$(grep -c 'dispatch_notification ' '$EWG') -ge 2 ]]"
check "dispatch in if cls==gate_human"  "awk '/if \\[\\[ \"\\\$cls\" == \"gate_human\" \\]\\]; then/,/^  fi/' '$EWG' | grep -q dispatch_notification"

echo "[3] orchestrator.sh"
check "sources lib/notify.sh"      "grep -q 'source.*lib/notify.sh' '$ORCH'"
check "resume re-invokes execute_with_gates.sh" "awk '/pending_gate present/,/esac/' '$ORCH' | grep -q 'execute_with_gates.sh'"
check "ewg_rc=1 pending_gate carve" "awk '/case \\\$ewg_rc/,/esac/' '$ORCH' | grep -q 'pending_gate_check'"
check "dispatch in INITIATIVE_COMPLETE branch" "awk '/INITIATIVE_COMPLETE: all completion_predicate/,/exit 0/' '$ORCH' | grep -q dispatch_notification"
check "dispatch in BUDGET_EXHAUSTED branch"    "awk '/log \"BUDGET_EXHAUSTED: stop_check/,/echo \"BUDGET_EXHAUSTED\"/' '$ORCH' | grep -q dispatch_notification"
check "dispatch in fail_counts threshold block" "awk '/fail_counts threshold/,/exit 3/' '$ORCH' | grep -q dispatch_notification"
check "dispatch_notification ≥ 4 total"  "[[ \$(grep -c 'dispatch_notification ' '$ORCH') -ge 4 ]]"

echo "[4] bash -n syntax"
check "lib/notify.sh syntax"       "bash -n '$NOTIFY'"
check "hooks/execute_with_gates.sh syntax" "bash -n '$EWG'"
check "orchestrator.sh syntax"     "bash -n '$ORCH'"

if (( fail == 0 )); then
  echo "ALL PASS"
  exit 0
else
  echo "FAILED"
  exit 1
fi
