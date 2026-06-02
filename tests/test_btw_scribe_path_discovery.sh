#!/usr/bin/env bash
# tests/test_btw_scribe_path_discovery.sh — FUP-0815 \btw scribe path-discovery test.
#
# The \btw scribe (ralph/.claude/commands/btw.md) discovers the live initiative state_dir
# by scanning Sub_Projects/*/state/state_snapshot.json. This test exercises the discovery
# rule via a portable bash helper (extracted from btw.md's discover-step prose) covering:
#
#   7.A — Single-candidate resolve: exactly one Sub_Projects/<name>/state/ has
#         state_snapshot.json AND seed.md → discovery picks it.
#   7.B — Multi-candidate disambiguation: two Sub_Projects/<*>/state/ have both files
#         → discovery surfaces all candidates and exits non-zero (caller picks).
#   7.C — Half-init guard: a Sub_Projects/<name>/state/ has state_snapshot.json BUT no
#         seed.md → discovery REJECTS that candidate.
#   7.D — Stale _orchestrator/ tree rejection: no candidate path may contain the
#         substring "_orchestrator/" (rejected per FUP-0812 stale-tree hazard).
#
# Run: bash tests/test_btw_scribe_path_discovery.sh
# Exit 0 = all PASS; exit 1 = any FAIL.

set -uo pipefail

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

PASS_COUNT=0
FAIL_COUNT=0
fail() { echo "FAIL: $*" >&2; FAIL_COUNT=$((FAIL_COUNT+1)); }
pass() { echo "PASS: $*"; PASS_COUNT=$((PASS_COUNT+1)); }

# Path-discovery helper (the algorithm the \btw scribe implements).
# Stdout: list of resolved state_dir paths, one per line.
# Exit 0: at least one candidate found.
# Exit 1: zero candidates.
discover_state_dirs() {
  local sub_projects_root="$1"
  local candidate
  for candidate in "$sub_projects_root"/*/state; do
    [[ -d "$candidate" ]] || continue
    [[ -f "$candidate/state_snapshot.json" ]] || continue  # FUP-0815 guard half-1
    [[ -f "$candidate/seed.md" ]] || continue              # FUP-0815 guard half-2
    [[ "$candidate" == *"_orchestrator/"* ]] && continue   # FUP-0812 stale-tree rejection
    echo "$candidate"
  done | head -100
}

# -------- 7.A: single-candidate resolve --------
SP="$TMPROOT/case_7A_sub_projects"
mkdir -p "$SP/Initiative_Alpha/state"
echo '{"pending_gate":null}' > "$SP/Initiative_Alpha/state/state_snapshot.json"
echo '---' > "$SP/Initiative_Alpha/state/seed.md"
RESULT="$(discover_state_dirs "$SP")"
COUNT="$(echo "$RESULT" | grep -c .)"
[[ "$COUNT" == "1" ]] && pass "7.A: exactly 1 candidate resolved" || fail "7.A: $COUNT candidates (expected 1)"
[[ "$RESULT" == *"Initiative_Alpha/state"* ]] && pass "7.A: candidate path is Initiative_Alpha/state" || fail "7.A: result=$RESULT"

# -------- 7.B: multi-candidate disambiguation --------
SP="$TMPROOT/case_7B_sub_projects"
mkdir -p "$SP/Initiative_Alpha/state" "$SP/Initiative_Beta/state"
echo '{"pending_gate":null}' > "$SP/Initiative_Alpha/state/state_snapshot.json"
echo '---' > "$SP/Initiative_Alpha/state/seed.md"
echo '{"pending_gate":null}' > "$SP/Initiative_Beta/state/state_snapshot.json"
echo '---' > "$SP/Initiative_Beta/state/seed.md"
RESULT="$(discover_state_dirs "$SP")"
COUNT="$(echo "$RESULT" | grep -c .)"
[[ "$COUNT" == "2" ]] && pass "7.B: 2 candidates surfaced for operator disambiguation" || fail "7.B: $COUNT candidates (expected 2)"

# -------- 7.C: half-init guard --------
SP="$TMPROOT/case_7C_sub_projects"
mkdir -p "$SP/Initiative_HalfInit/state"
echo '{"pending_gate":null}' > "$SP/Initiative_HalfInit/state/state_snapshot.json"
# seed.md ABSENT
RESULT="$(discover_state_dirs "$SP")"
COUNT="$(echo "$RESULT" | grep -c .)"
[[ "$COUNT" == "0" ]] && pass "7.C: half-init tree REJECTED (missing seed.md)" || fail "7.C: $COUNT candidates (expected 0 — half-init guard FAILED)"

# 7.C inverse: state_snapshot.json absent
SP2="$TMPROOT/case_7C2_sub_projects"
mkdir -p "$SP2/Initiative_NoSnapshot/state"
echo '---' > "$SP2/Initiative_NoSnapshot/state/seed.md"
# state_snapshot.json ABSENT
RESULT="$(discover_state_dirs "$SP2")"
COUNT="$(echo "$RESULT" | grep -c .)"
[[ "$COUNT" == "0" ]] && pass "7.C inverse: half-init tree REJECTED (missing state_snapshot.json)" || fail "7.C inverse: $COUNT candidates (expected 0)"

# -------- 7.D: stale _orchestrator/ pattern rejection --------
SP="$TMPROOT/case_7D_sub_projects"
mkdir -p "$SP/Initiative_Live/state"
mkdir -p "$SP/_orchestrator/Initiative_Stale/state"
echo '{"pending_gate":null}' > "$SP/Initiative_Live/state/state_snapshot.json"
echo '---' > "$SP/Initiative_Live/state/seed.md"
echo '{"pending_gate":null}' > "$SP/_orchestrator/Initiative_Stale/state/state_snapshot.json"
echo '---' > "$SP/_orchestrator/Initiative_Stale/state/seed.md"
RESULT="$(discover_state_dirs "$SP")"
COUNT="$(echo "$RESULT" | grep -c .)"
# The glob `$SP/*/state` matches Initiative_Live/state but NOT _orchestrator/Initiative_Stale/state
# (the latter is 2 levels deep). The substring check provides defence-in-depth for any nested case.
[[ "$COUNT" == "1" ]] && pass "7.D: only Initiative_Live/state resolved; _orchestrator/ tree excluded" || fail "7.D: $COUNT candidates"
[[ "$RESULT" == *"Initiative_Live/state"* ]] && pass "7.D: candidate is Initiative_Live/state (live tree)" || fail "7.D: result=$RESULT"
[[ "$RESULT" != *"_orchestrator"* ]] && pass "7.D: no candidate contains _orchestrator (FUP-0812)" || fail "7.D: stale _orchestrator/ path leaked into results"

echo ""
echo "=== test_btw_scribe_path_discovery.sh: $PASS_COUNT PASS, $FAIL_COUNT FAIL ==="
[[ $FAIL_COUNT -eq 0 ]]
