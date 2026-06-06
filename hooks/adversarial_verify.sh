#!/usr/bin/env bash
# T3#7: opt-in adversarial second-opinion on an integration_checkpoint close.
#
# The Consumer substrate-verifies a close (strong), but a confidently-wrong close of
# an expensive checkpoint can still slip through. When the seed opts in
# (`adversarial_verify_on_checkpoint_close: true`), this hook runs ONE independent
# `claude -p` pass over the just-written execution report, prompted to REFUTE the
# close — find a real defect (a test that wouldn't pass, an un-pushed commit, a
# closed-seam edit, a fabricated facet, a missing deliverable). If the refuter finds
# a genuine defect it writes a gate_human escalation and exits 3 (the orchestrator
# treats this as "do not trust the close"); otherwise it exits 0.
#
# DEFAULT OFF: with the seed flag absent/false this is a no-op (exit 0), so the loop
# is byte-identical unless the operator opts in. Only fires for integration_checkpoint
# shapes that produced a report. RALPH_CLAUDE overrides the CLI (test seam).
#
# Usage: adversarial_verify.sh <seed_path> <iter_dir>

set -uo pipefail

SEED="${1:?usage: adversarial_verify.sh <seed_path> <iter_dir>}"
ITER_DIR="${2:?usage: adversarial_verify.sh <seed_path> <iter_dir>}"
ITER="$(basename "$ITER_DIR")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE="${RALPH_CLAUDE:-claude}"

# --- opt-in gate -------------------------------------------------------------
ENABLED=""
if [[ -f "$SCRIPT_DIR/../lib/seed.sh" ]]; then
  # shellcheck source=/dev/null
  source "$SCRIPT_DIR/../lib/seed.sh"
  ENABLED="$(read_seed_field "$SEED" .adversarial_verify_on_checkpoint_close 2>/dev/null || echo "")"
fi
if [[ "$ENABLED" != "true" ]]; then
  echo "adversarial_verify: not enabled (seed flag absent/false) — skipping." >&2
  exit 0
fi

# --- checkpoint-shape + report preconditions ---------------------------------
PLAN_PATH="$ITER_DIR/session_plan_${ITER}.md"
_shape=""
if [[ -f "$PLAN_PATH" ]]; then
  _shape="$(sed -nE 's/^[[:space:]]*(-[[:space:]]+)?\*{0,2}shape:\*{0,2}[[:space:]]*([A-Za-z_]+).*/\2/p' "$PLAN_PATH" 2>/dev/null | head -1)"
fi
if [[ "$_shape" != "integration_checkpoint" ]]; then
  echo "adversarial_verify: shape='$_shape' (not integration_checkpoint) — skipping." >&2
  exit 0
fi

REPORT="$ITER_DIR/execution_report_${ITER}.md"
if [[ ! -f "$REPORT" ]]; then
  echo "adversarial_verify: no execution_report_${ITER}.md — nothing to refute, skipping." >&2
  exit 0
fi

# --- one independent refute pass ---------------------------------------------
PROMPT="You are an adversarial reviewer auditing a Ralph Loop iteration that claims to have CLOSED one or more registry work-items at an integration checkpoint. Try HARD to REFUTE the close: look for a genuine defect — a claimed cf-pytest result that would not actually pass, an un-pushed or missing commit/tag, an edit to a closed component seam, a fabricated or unverifiable predicate facet, or a missing deliverable. Respond with EXACTLY one JSON object and nothing else: {\"refuted\": true|false, \"reason\": \"<one sentence>\"}. Set refuted=true only if you found a SPECIFIC, real defect; set refuted=false if the close looks sound. Here is the iteration's report:

$(cat "$REPORT")"

OUT="$("$CLAUDE" --print --output-format json <<<"$PROMPT" 2>/dev/null || echo '')"
RESULT_TEXT="$(jq -r '.result // empty' <<<"$OUT" 2>/dev/null || echo "")"
# Fall back to the raw output if it was not a JSON envelope (stub / plain text).
[[ -z "$RESULT_TEXT" ]] && RESULT_TEXT="$OUT"

if grep -qiE '"refuted"[[:space:]]*:[[:space:]]*true' <<<"$RESULT_TEXT"; then
  ESC_DIR="${STATE_DIR:-$ITER_DIR}/escalations"
  mkdir -p "$ESC_DIR"
  ESC="$ESC_DIR/adversarial_refutation_${ITER}.json"
  reason="$(grep -oiE '"reason"[[:space:]]*:[[:space:]]*"[^"]*"' <<<"$RESULT_TEXT" | head -1)"
  jq -n --arg iter "$ITER" --arg reason "$reason" --arg report "$REPORT" \
    '{iteration:$iter, classification:"adversarial_refutation", class_hint:"gate_human", reason:$reason, report:$report}' \
    > "$ESC" 2>/dev/null || printf '{"iteration":"%s","classification":"adversarial_refutation","class_hint":"gate_human"}\n' "$ITER" > "$ESC"
  echo "adversarial_verify: REFUTED — the second-opinion pass found a defect ($reason); escalation $ESC. Do NOT trust the close." >&2
  exit 3
fi

echo "adversarial_verify: CONFIRMED — the second-opinion pass found no defect in the $ITER close." >&2
exit 0
