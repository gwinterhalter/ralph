#!/usr/bin/env bash
# hooks/plan_review.sh <plan_path>   (Initiative_Orchestrator_Spec §13.2)
# Orchestrates the cf-session-plan-reviewer + rl-initiative-planner --revise inner loop, <=5 rounds.
# Exit 0 — reviewer returned zero findings within 5 rounds
# Exit 1 — did not converge in 5 rounds; escalation file written; orchestrator should block on gate_human
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/seed.sh
source "$SCRIPT_DIR/../lib/seed.sh"
# shellcheck source=../lib/events.sh
# FUP-0842: event-log emit helper for the revise_round emit in the inner loop below.
source "$SCRIPT_DIR/../lib/events.sh"

PLAN_PATH="${1:?usage: plan_review.sh <plan_path>}"
ITER_DIR="$(dirname "$PLAN_PATH")"
STATE_DIR="$(dirname "$(dirname "$ITER_DIR")")"   # iterations/NNNN -> state_dir
ITER="$(basename "$ITER_DIR")"
# FUP-0842 C.0: bind the §4.1 project_id + slug (prefer orchestrator-exported values; fall back to
# seed-derived for standalone invocation). Reused at the revise_round emit site.
EVENT_PROJECT_ID="${EVENT_PROJECT_ID:-}"
EVENT_SLUG="${EVENT_SLUG:-}"
if [[ -z "$EVENT_SLUG" || "$EVENT_SLUG" == "null" ]]; then
  EVENT_SLUG="$(read_seed_field "$STATE_DIR/seed.md" .initiative.slug 2>/dev/null || echo "")"
fi
if [[ -z "$EVENT_PROJECT_ID" || "$EVENT_PROJECT_ID" == "null" ]]; then
  EVENT_PROJECT_ID="$(read_seed_field "$STATE_DIR/seed.md" .initiative.project_id 2>/dev/null || echo "")"
  [[ -z "$EVENT_PROJECT_ID" || "$EVENT_PROJECT_ID" == "null" ]] && EVENT_PROJECT_ID="$EVENT_SLUG"
fi

# FUP-0720 cost instrumentation: read budget cap from seed; default to 20 USD on failure.
SEED_PATH="$STATE_DIR/seed.md"
BUDGET_CAP=$(read_seed_field "$SEED_PATH" .budget.tokens_usd 2>/dev/null || echo 20)
[[ -z "$BUDGET_CAP" || "$BUDGET_CAP" == "null" ]] && BUDGET_CAP=20

for round in 1 2 3 4 5; do
  FINDINGS="$ITER_DIR/review_findings_${round}.json"
  RESULT_TEXT="$ITER_DIR/review_result_${round}.txt"
  # Invoke cf-session-plan-reviewer; capture JSON envelope via --output-format json. FUP-0720.
  # FUP-0743: --add-dir "$CLAUDE_SKILLS_DIR" -- required so the slash command resolves from
  # the ralph/ CWD (skills live in a SIBLING tree); env var exported by orchestrator.sh.
  claude -p --output-format json --max-budget-usd "$BUDGET_CAP" \
    --add-dir "$CLAUDE_SKILLS_DIR" -- "/cf-session-plan-reviewer $PLAN_PATH" > "$FINDINGS"
  # Extract .result field for convergence regex (newlines unescaped) — FUP-0720.
  jq -r '.result // empty' "$FINDINGS" > "$RESULT_TEXT"
  # Converged when the reviewer emits its completion block reporting zero BLOCKER
  # and zero DRIFT findings. cf-session-plan-reviewer Delivery Format contract:
  # a "## Session Plan Review Complete" header followed by a
  # "- Findings: N BLOCKER, N DRIFT, N COSMETIC" summary line. COSMETIC findings
  # do NOT block convergence (reviewer Step 6 severity model). FUP-0716 / FUP-0719.
  # FUP-0770: the reviewer may emit the Findings/header line wrapped in markdown
  # bold (e.g. "- **Findings: 0 BLOCKER, 0 DRIFT, 0 COSMETIC**"); strip all '*'
  # before the convergence greps so bold anywhere on the line cannot block a match.
  CLEAN_RESULT="$(sed 's/\*//g' "$RESULT_TEXT")"
  # FUP-0842: revise_round — one event per plan_review ↔ planner --revise inner-loop round (§13 Q8
  # loop churn). Parse the reviewer's "Findings: N BLOCKER, N DRIFT" summary (best-effort; -1 when
  # unparseable); verdict=converged when the convergence regex matches, else revise. role=planner.
  rr_converged=0
  if printf '%s' "$CLEAN_RESULT" | grep -qF '## Session Plan Review Complete' \
     && printf '%s' "$CLEAN_RESULT" | grep -qE '^-[[:space:]]*Findings:[[:space:]]*0[[:space:]]+BLOCKER,[[:space:]]*0[[:space:]]+DRIFT'; then
    rr_converged=1
  fi
  rr_fb="$(printf '%s' "$CLEAN_RESULT" | grep -oE 'Findings:[[:space:]]*[0-9]+[[:space:]]+BLOCKER' | grep -oE '[0-9]+' | head -1)"
  rr_fd="$(printf '%s' "$CLEAN_RESULT" | grep -oE '[0-9]+[[:space:]]+DRIFT' | grep -oE '[0-9]+' | head -1)"
  [[ -z "$rr_fb" ]] && rr_fb=-1
  [[ -z "$rr_fd" ]] && rr_fd=-1
  rr_verdict="revise"; [[ "$rr_converged" -eq 1 ]] && rr_verdict="converged"
  emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "planner" "revise_round" "" "" "" \
    "$(jq -nc --argjson rd "$round" --argjson fb "$rr_fb" --argjson fd "$rr_fd" --arg v "$rr_verdict" \
       '{round:$rd, findings_blocker:$fb, findings_drift:$fd, verdict:$v}')"
  if [[ "$rr_converged" -eq 1 ]]; then
    exit 0
  fi
  # Else invoke planner --revise to produce a revised plan (overwrites $PLAN_PATH).
  # FUP-0743: --add-dir + -- (same rationale as the reviewer call above).
  claude -p --output-format json --max-budget-usd "$BUDGET_CAP" \
    --add-dir "$CLAUDE_SKILLS_DIR" -- "/rl-initiative-planner --revise $FINDINGS" > "$ITER_DIR/revise_round_${round}.json"
done

# Did not converge — write escalation (§13.2 exit 1).
mkdir -p "$STATE_DIR/escalations"
ESCALATION="$STATE_DIR/escalations/plan_review_nonconvergence_$(basename "$ITER_DIR").md"
cat > "$ESCALATION" <<EOF
# Plan review non-convergence

Plan: $PLAN_PATH
Did not converge after 5 cf-session-plan-reviewer rounds.
Final findings: $ITER_DIR/review_findings_5.json
Action: operator gate_human decision required.
EOF
exit 1
