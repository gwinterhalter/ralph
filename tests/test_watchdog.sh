#!/usr/bin/env bash
# T2#3 — watchdog.sh restart/stop behaviour, driven by a stub orchestrator injected
# via RALPH_ORCHESTRATOR. Backoff 0 so the test is fast.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHDOG="$RALPH_DIR/watchdog.sh"

PASS=0; FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*" >&2; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STATE_DIR="$WORK/state"
mkdir -p "$STATE_DIR/logs"

# Minimal seed the watchdog's seed.sh resolution can parse.
SEED="$WORK/seed.md"
cat > "$SEED" <<EOF
---
workspace_root: "$WORK"
state_dir_relative: "state"
---
# stub seed
EOF

# Stub orchestrator: behaviour by \$STUB_MODE, run count in \$STUB_COUNT.
STUB="$WORK/stub_orchestrator.sh"
cat > "$STUB" <<'STUBEOF'
#!/usr/bin/env bash
log="$STATE_DIR/logs/orchestrator.log"
mkdir -p "$STATE_DIR/logs"
n=$(( $(cat "$STUB_COUNT" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$STUB_COUNT"
case "$STUB_MODE" in
  crash_then_complete)
    if [[ "$n" -lt 3 ]]; then echo "ITERATION 000$n crashed mid-run" >> "$log"; exit 1
    else echo "INITIATIVE_COMPLETE: all completion_predicate[] passed" >> "$log"; exit 0; fi ;;
  complete_first) echo "INITIATIVE_COMPLETE: all completion_predicate[] passed" >> "$log"; exit 0 ;;
  always_crash) echo "ITERATION 000$n crashed mid-run" >> "$log"; exit 1 ;;
esac
STUBEOF
chmod +x "$STUB"

export STATE_DIR STUB

run_watchdog() {  # $1=mode  $2=max_restarts
  : > "$STATE_DIR/logs/orchestrator.log"
  echo 0 > "$WORK/count"
  STUB_MODE="$1" STUB_COUNT="$WORK/count" RALPH_ORCHESTRATOR="$STUB" \
    bash "$WATCHDOG" "$SEED" "$2" 0 >/dev/null 2>&1
  echo "$?"
}

# 1. crash twice, then INITIATIVE_COMPLETE -> stops 0, stub called 3x.
rc="$(run_watchdog crash_then_complete 3)"; n="$(cat "$WORK/count")"
[[ "$rc" == "0" ]] && pass "crash_then_complete exits 0" || fail "crash_then_complete rc=$rc (expected 0)"
[[ "$n" == "3" ]] && pass "crash_then_complete ran orchestrator 3x (2 restarts + complete)" || fail "ran ${n}x (expected 3)"

# 2. INITIATIVE_COMPLETE on first run -> stops 0, no restart.
rc="$(run_watchdog complete_first 3)"; n="$(cat "$WORK/count")"
[[ "$rc" == "0" && "$n" == "1" ]] && pass "complete_first exits 0 after one run" || fail "complete_first rc=$rc n=$n (expected 0/1)"

# 3. always crash -> gives up after MAX_RESTARTS (=2) -> exit 1, stub called 3x.
rc="$(run_watchdog always_crash 2)"; n="$(cat "$WORK/count")"
[[ "$rc" == "1" ]] && pass "always_crash gives up exit 1" || fail "always_crash rc=$rc (expected 1)"
[[ "$n" == "3" ]] && pass "always_crash ran orchestrator 3x (1 + 2 restarts)" || fail "ran ${n}x (expected 3)"

echo "=== test_watchdog.sh: $PASS PASS, $FAIL FAIL ==="
[[ "$FAIL" -eq 0 ]]
