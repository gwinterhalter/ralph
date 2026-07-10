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

# Cheap-role model (concurrency Tier-1 follow-on, 2026-06-09): plan review — both the
# cf-session-plan-reviewer pass and the rl-initiative-planner --revise pass — is a cheap,
# bounded role, so it runs on a cheaper model by default to stretch the scarce Opus weekly
# allowance (the real Max governor). Seed override: `.plan_review_model`; default "sonnet"
# when unset/null. An explicit empty value would fall through to the CLI default.
PLAN_REVIEW_MODEL="$(read_seed_field "$SEED_PATH" .plan_review_model 2>/dev/null || echo "")"
[[ -z "$PLAN_REVIEW_MODEL" || "$PLAN_REVIEW_MODEL" == "null" ]] && PLAN_REVIEW_MODEL="sonnet"
PLAN_REVIEW_MODEL_FLAG=""
[[ -n "$PLAN_REVIEW_MODEL" ]] && PLAN_REVIEW_MODEL_FLAG="--model $PLAN_REVIEW_MODEL"

# Transient-resilience (2026-06-11): a single `claude` call can exit non-zero on a transient
# rate-limit / usage / network blip. Under `set -euo pipefail` that abort propagated out of this
# hook and was misreported by orchestrator.sh as plan_review *non-convergence* — a spurious
# gate_human block (the genuine <=5-round non-convergence path writes an escalation file; a
# transient abort does NOT, so the two were indistinguishable to the orchestrator). Retry the call
# up to 3 attempts with linear backoff; only a persistent failure propagates. Usage:
#   run_claude_retry <out_file> <claude args...>
run_claude_retry() {
  local _out="$1"; shift
  local _attempt _rc=0
  for _attempt in 1 2 3; do
    _rc=0
    claude "$@" > "$_out" || _rc=$?
    [[ "$_rc" -eq 0 ]] && return 0
    if [[ "$_attempt" -lt 3 ]]; then sleep $(( _attempt * 15 )); fi
  done
  return "$_rc"
}

for round in 1 2 3 4 5; do
  FINDINGS="$ITER_DIR/review_findings_${round}.json"
  RESULT_TEXT="$ITER_DIR/review_result_${round}.txt"
  # Invoke cf-session-plan-reviewer; capture JSON envelope via --output-format json. FUP-0720.
  # FUP-0743: --add-dir "$CLAUDE_SKILLS_DIR" -- required so the slash command resolves from
  # the ralph/ CWD (skills live in a SIBLING tree); env var exported by orchestrator.sh.
  # shellcheck disable=SC2086  # PLAN_REVIEW_MODEL_FLAG is intentionally word-split (flag + value or empty)
  # (2026-07-01) Capture the reviewer's exit code instead of letting `set -e` abort here: a
  # `claude -p` reviewer call can exit NON-ZERO while still writing a valid KEEP result envelope
  # (flaky/transient exit, or a benign budget/tool warning). Un-guarded under `set -e` that aborted
  # plan_review.sh with rc=1 BEFORE the convergence check, which orchestrator.sh then mis-reported as
  # plan_review non-convergence -> a spurious gate_human (observed 2026-07-01: S4 reviewer said "KEEP,
  # 0 BLOCKER ... ready for Executor dispatch" yet the run blocked). run_claude_retry already retries
  # 3x; here we simply DON'T abort on its final rc — the convergence check below decides on the
  # ACTUAL review output (converge on a clean KEEP; fall through to --revise on empty/genuine findings).
  review_rc=0
  run_claude_retry "$FINDINGS" -p $PLAN_REVIEW_MODEL_FLAG --output-format json --max-budget-usd "$BUDGET_CAP" \
    --add-dir "$CLAUDE_SKILLS_DIR" -- "/cf-session-plan-reviewer $PLAN_PATH" || review_rc=$?
  [[ "$review_rc" -ne 0 ]] && echo "plan_review: reviewer round $round exited rc=$review_rc (output still evaluated for convergence)" >&2
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
  # FUP-0770 + (2026-06-10) loop-convergence fix: parse the reviewer's
  # "Findings: N BLOCKER, N DRIFT" counts FIRST, then converge whenever BLOCKER==0 AND
  # DRIFT==0 under a real completion summary — keyed on the PARSED counts, not an exact
  # line-format regex. Previously a clean 0/0 review whose Findings line varied in
  # spacing/bold/leading-char failed the strict regex and looped to the 5-round cap
  # despite zero blocking findings. COSMETIC never blocks (reviewer Step 6 model).
  rr_fb="$(printf '%s' "$CLEAN_RESULT" | grep -oE 'Findings:[[:space:]]*[0-9]+[[:space:]]+BLOCKER' | grep -oE '[0-9]+' | head -1)"
  rr_fd="$(printf '%s' "$CLEAN_RESULT" | grep -oE '[0-9]+[[:space:]]+DRIFT' | grep -oE '[0-9]+' | head -1)"
  [[ -z "$rr_fb" ]] && rr_fb=-1
  [[ -z "$rr_fd" ]] && rr_fd=-1
  # (2026-07-01) Additive convergence route — recognise an explicit reviewer KEEP verdict.
  # cf-session-plan-reviewer emits "Recommendation/Conclusion: KEEP" (its verdict that the plan is
  # ready for Executor dispatch) WITHOUT the exact "## Session Plan Review Complete" + "Findings: N
  # BLOCKER" Delivery-Format line for fr_extraction surface-(c) plans, so the strict path below never
  # fired and a clean 0-blocker review looped to the 5-round cap into a spurious plan_review
  # non-convergence gate_human (observed 2026-07-01, CF_Re-Arch_M4G rerun). The reviewer cites
  # BLOCKER/DRIFT counts in prose too, so count-parsing is unreliable; we key on the KEEP verdict
  # itself (which per the reviewer's Step-6 contract means zero blocking findings remain), guarded
  # against a REVISE verdict. This ONLY ADDS a route — the strict marker+counts path is untouched.
  # Positive KEEP signals (any) — the reviewer varies its verdict lead-in across plans/iterations
  # (observed: "Recommendation: KEEP", "Conclusion: KEEP", "Result: KEEP — 0 BLOCKER", "review
  # complete — KEEP", plus the standing "ready for Executor dispatch" readiness statement). We do
  # NOT count BLOCKER/DRIFT mentions — the reviewer discusses findings it has already FIXED in prose,
  # so counts are unreliable in both directions. Guarded against an explicit REVISE verdict.
  rr_keep=0; rr_revise=0
  printf '%s' "$CLEAN_RESULT" | grep -qiE '(recommendation|conclusion|verdict|result|bottom line)[[:space:]:.-]*keep([^a-zA-Z0-9]|$)' && rr_keep=1
  printf '%s' "$CLEAN_RESULT" | grep -qiE 'ready for (the )?(executor|dispatch)' && rr_keep=1
  printf '%s' "$CLEAN_RESULT" | grep -qiE '(recommendation|conclusion|verdict|result)[[:space:]:.-]*revise([^a-zA-Z0-9]|$)' && rr_revise=1
  rr_converged=0
  if printf '%s' "$CLEAN_RESULT" | grep -qF '## Session Plan Review Complete' \
     && [[ "$rr_fb" -eq 0 && "$rr_fd" -eq 0 ]]; then
    rr_converged=1
  elif [[ "$rr_keep" -eq 1 && "$rr_revise" -eq 0 ]]; then
    rr_converged=1
  fi
  rr_verdict="revise"; [[ "$rr_converged" -eq 1 ]] && rr_verdict="converged"
  # Item-2 FR-052: stamp the session-plan shape on the revise_round event so the Run-Auditor's
  # learn_assembly can recover per-shape revision rates (which shapes keep needing reviewer
  # revision). Same shape-extraction sed used by execute_with_gates / adversarial_verify; empty
  # when the plan declares no shape line (the learning pass then skips this use).
  rr_shape="$(sed -nE 's/^[[:space:]]*(-[[:space:]]+)?\*{0,2}shape:\*{0,2}[[:space:]]*([A-Za-z_]+).*/\2/p' "$PLAN_PATH" 2>/dev/null | head -1)"
  emit_event "$STATE_DIR" "$EVENT_PROJECT_ID" "$EVENT_SLUG" "$ITER" "planner" "revise_round" "" "" "" \
    "$(jq -nc --argjson rd "$round" --argjson fb "$rr_fb" --argjson fd "$rr_fd" --arg v "$rr_verdict" --arg sh "$rr_shape" \
       '{round:$rd, findings_blocker:$fb, findings_drift:$fd, verdict:$v} + (if $sh != "" then {shape:$sh} else {} end)')"
  if [[ "$rr_converged" -eq 1 ]]; then
    exit 0
  fi
  # Else invoke planner --revise to produce a revised plan (overwrites $PLAN_PATH).
  # FUP-0743: --add-dir + -- (same rationale as the reviewer call above).
  # shellcheck disable=SC2086  # PLAN_REVIEW_MODEL_FLAG is intentionally word-split (flag + value or empty)
  # (2026-07-01) Same set -e guard as the reviewer call: don't abort on a non-zero exit from the
  # --revise call when it still overwrote the plan; the next round's reviewer evaluates the result.
  revise_rc=0
  run_claude_retry "$ITER_DIR/revise_round_${round}.json" -p $PLAN_REVIEW_MODEL_FLAG --output-format json --max-budget-usd "$BUDGET_CAP" \
    --add-dir "$CLAUDE_SKILLS_DIR" -- "/rl-initiative-planner --revise $FINDINGS" || revise_rc=$?
  [[ "$revise_rc" -ne 0 ]] && echo "plan_review: --revise round $round exited rc=$revise_rc (continuing to next review round)" >&2
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
