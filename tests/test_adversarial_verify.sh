#!/usr/bin/env bash
# T3#7 — adversarial_verify.sh opt-in refute pass, driven by a stub claude via
# RALPH_CLAUDE. Verifies: flag-off no-op, non-checkpoint skip, confirmed pass,
# refuted -> escalation + exit 3.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="$RALPH_DIR/hooks/adversarial_verify.sh"

PASS=0; FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*" >&2; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
export STATE_DIR="$WORK/state"
ITER_DIR="$STATE_DIR/iterations/0099"
mkdir -p "$ITER_DIR"

# A checkpoint plan + report (the close to audit).
printf -- '- **shape:** integration_checkpoint\n' > "$ITER_DIR/session_plan_0099.md"
printf '# Report\n\n## Items closed\n- OLB-99 — demo\n' > "$ITER_DIR/execution_report_0099.md"

mkseed() {  # $1=flag-value ("true"/"false")
  cat > "$WORK/seed.md" <<EOF
---
adversarial_verify_on_checkpoint_close: $1
---
# seed
EOF
}

# Stub claude: emits an --output-format json envelope whose .result is the verdict.
STUB="$WORK/stub_claude.sh"
cat > "$STUB" <<'STUBEOF'
#!/usr/bin/env bash
cat >/dev/null   # consume the prompt on stdin
printf '{"result": "%s", "is_error": false}\n' "$STUB_VERDICT"
STUBEOF
chmod +x "$STUB"

run() { RALPH_CLAUDE="$STUB" STUB_VERDICT="$1" bash "$HOOK" "$WORK/seed.md" "$ITER_DIR"; echo "$?"; }

# 1. flag false -> no-op exit 0 (stub verdict irrelevant).
mkseed false
rc="$(STUB_VERDICT='{\"refuted\": true}' run '{\"refuted\": true}')"
[[ "$rc" == "0" ]] && pass "flag false -> skip exit 0" || fail "flag false rc=$rc"
[[ ! -f "$STATE_DIR/escalations/adversarial_refutation_0099.json" ]] && pass "flag false -> no escalation" || fail "flag false wrote escalation"

# 2. flag true + confirmed -> exit 0, no escalation.
mkseed true
rc="$(run '{\"refuted\": false, \"reason\": \"looks sound\"}')"
[[ "$rc" == "0" ]] && pass "confirmed -> exit 0" || fail "confirmed rc=$rc"
[[ ! -f "$STATE_DIR/escalations/adversarial_refutation_0099.json" ]] && pass "confirmed -> no escalation" || fail "confirmed wrote escalation"

# 3. flag true + refuted -> exit 3 + escalation written.
rc="$(run '{\"refuted\": true, \"reason\": \"cf-pytest count cannot be reproduced\"}')"
[[ "$rc" == "3" ]] && pass "refuted -> exit 3" || fail "refuted rc=$rc (expected 3)"
[[ -f "$STATE_DIR/escalations/adversarial_refutation_0099.json" ]] && pass "refuted -> escalation written" || fail "refuted no escalation"

# 4. non-checkpoint shape -> skip even when enabled + refuted.
printf -- '- **shape:** component_build\n' > "$ITER_DIR/session_plan_0099.md"
rm -f "$STATE_DIR/escalations/adversarial_refutation_0099.json"
rc="$(run '{\"refuted\": true}')"
[[ "$rc" == "0" ]] && pass "component_build -> skip exit 0" || fail "component_build rc=$rc"
[[ ! -f "$STATE_DIR/escalations/adversarial_refutation_0099.json" ]] && pass "component_build -> no escalation" || fail "component_build wrote escalation"

echo "=== test_adversarial_verify.sh: $PASS PASS, $FAIL FAIL ==="
[[ "$FAIL" -eq 0 ]]
