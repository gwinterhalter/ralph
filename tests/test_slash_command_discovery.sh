#!/usr/bin/env bash
# test_slash_command_discovery.sh — regression test for FUP-0739 + FUP-0740 patch
# (orchestrator.sh `claude -p` invocation flags for slash-command discovery + MSYS env guards).
#
# Two parts:
#   1) Static guard (always runs; zero spend) — asserts orchestrator.sh retains the patch.
#   2) Behavioral check (--live; ~$0.05–0.30 spend; requires network + auth) — reproduces
#      Diagnostic 3 from Ralph_Loop_FUP-0717_0718_0737_R4_Gating_Fix_Execution_Report_2026-05-26_v1.0.md
#      §3, asserting `claude -p` resolves /rl-initiative-planner from the ralph/ CWD.
#
# Defect class regression: without --add-dir on the claude -p invocation, the CLI's
# slash-command resolver cannot find the rl-* skills (they live in a sibling tree, not
# an ancestor of ralph/), and `claude -p "/rl-initiative-planner …"` short-circuits with
# result="Unknown command: /rl-initiative-planner" in ~11 ms / 0 turns. Additionally,
# without MSYS_NO_PATHCONV=1 on Git Bash for Windows, the /rl-* prompt argument is
# rewritten to C:/Program Files/Git/rl-initiative-planner and K:-drive paths get
# colon-split into MSYS PATH-list form, mangling the args before claude receives them.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ORCH="$RALPH_ROOT/orchestrator.sh"

LIVE=0
for arg in "$@"; do
  case "$arg" in
    --live) LIVE=1 ;;
    *) echo "WARN: unknown arg '$arg' (recognised: --live)" >&2 ;;
  esac
done

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Part 1 — static guard (zero spend)
# ---------------------------------------------------------------------------
echo "[static] checking orchestrator.sh retains FUP-0739 + FUP-0740 patch..."

[[ -f "$ORCH" ]] || fail "orchestrator.sh not found at $ORCH"

grep -qF 'export MSYS_NO_PATHCONV=1' "$ORCH" \
  || fail "FUP-0740 guard 'export MSYS_NO_PATHCONV=1' missing from orchestrator.sh"

grep -qF "export MSYS2_ARG_CONV_EXCL='*'" "$ORCH" \
  || fail "FUP-0740 guard \"export MSYS2_ARG_CONV_EXCL='*'\" missing from orchestrator.sh"

grep -qF ': "${CLAUDE_SKILLS_DIR:=' "$ORCH" \
  || fail "FUP-0739 default-assignment ': \"\${CLAUDE_SKILLS_DIR:=...}\"' missing from orchestrator.sh"

grep -qF -- '--add-dir "$CLAUDE_SKILLS_DIR" --' "$ORCH" \
  || fail "FUP-0739 '--add-dir \"\$CLAUDE_SKILLS_DIR\" --' missing from orchestrator.sh claude -p invocation"

# Negative: the pre-patch invocation form must NOT remain.
if grep -qF -- '--max-budget-usd "$remaining_budget" "$@"' "$ORCH"; then
  fail "pre-patch claude -p form '--max-budget-usd \"\$remaining_budget\" \"\$@\"' (no --add-dir) still present in orchestrator.sh"
fi

echo "[static] PASS — all 4 patch markers present + pre-patch form absent"

# ---------------------------------------------------------------------------
# Part 2 — behavioral check (--live only)
# ---------------------------------------------------------------------------
if [[ "$LIVE" -ne 1 ]]; then
  echo "[behavioral] skipped (pass --live to run; ~\$0.05-0.30 spend, requires auth)"
  exit 0
fi

command -v claude >/dev/null 2>&1 || fail "claude CLI not on PATH"
command -v jq     >/dev/null 2>&1 || fail "jq not on PATH"

CLAUDE_SKILLS_DIR="${CLAUDE_SKILLS_DIR:-K:/Claude Code Factory/V3/Project_Docs}"

echo "[behavioral] invoking claude -p from $RALPH_ROOT with --add-dir $CLAUDE_SKILLS_DIR ..."

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

cd "$RALPH_ROOT"

MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
  claude -p --output-format json --max-budget-usd 0.05 \
    --add-dir "$CLAUDE_SKILLS_DIR" -- "/rl-initiative-planner --version-info" \
  > "$TMP" || true   # the --max-budget-usd cap may trip is_error=true; that's fine

RESULT="$(jq -r '.result // ""' "$TMP")"

if [[ "$RESULT" == *"Unknown command"* ]]; then
  echo "[behavioral] FAIL — claude -p returned 'Unknown command' (slash-command discovery still broken):"
  echo "  $RESULT" | head -c 400
  echo ""
  exit 1
fi

echo "[behavioral] PASS — /rl-initiative-planner resolved (no 'Unknown command' in result)"
exit 0
