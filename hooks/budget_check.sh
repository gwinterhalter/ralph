#!/usr/bin/env bash
# hooks/budget_check.sh <seed_path> <state_dir>   (Initiative_Orchestrator_Spec §13.2)
# Exit 0 — within both budgets
# Exit 1 — budget exhausted (orchestrator terminates BUDGET_EXHAUSTED)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/seed.sh
source "$SCRIPT_DIR/../lib/seed.sh"

SEED="${1:?usage: budget_check.sh <seed_path> <state_dir>}"
STATE_DIR="${2:?usage: budget_check.sh <seed_path> <state_dir>}"

ITER_MAX="$(read_seed_field "$SEED" '.budget.iterations_max')"
TOKENS_USD_MAX="$(read_seed_field "$SEED" '.budget.tokens_usd')"

# (2026-08-03) FUP-0815 override was honoured by ONLY ONE of the two budget checks.
# orchestrator.sh:136-142 computes effective_cap = max(seed cap, budget_override.json) before
# each `claude -p`, but THIS post-iteration check read the seed cap directly -- so an operator
# who bumped the budget via the documented mechanism got a run that sailed past the seed cap
# mid-iteration and was then killed by this check at the end of it. The override was effectively
# inert: observed live on factory_dryrun run 2, override=400, halted BUDGET_EXHAUSTED at
# spend=70.42 against the seed's 60. Mirror the orchestrator's max() so both checks agree --
# a cap enforced by two checks that disagree is not a cap, it is a race between them.
if [[ -f "$STATE_DIR/budget_override.json" ]]; then
  _ovr="$(jq -r '.budget_cap_usd // empty' "$STATE_DIR/budget_override.json" 2>/dev/null || true)"
  if [[ -n "$_ovr" ]]; then
    TOKENS_USD_MAX="$(awk -v a="$TOKENS_USD_MAX" -v b="$_ovr" 'BEGIN{print (b>a)?b:a}')"
  fi
fi

# iterations_max: count iterations/NNNN/ subdirs
iter_count=0
if [[ -d "$STATE_DIR/iterations" ]]; then
  iter_count=$(find "$STATE_DIR/iterations" -maxdepth 1 -type d -regex '.*/[0-9][0-9][0-9][0-9]$' | wc -l | tr -d ' ')
fi
if [[ "$iter_count" -ge "$ITER_MAX" ]]; then
  echo "budget_check: iterations_max reached ($iter_count >= $ITER_MAX)" >&2
  exit 1
fi

# tokens_usd (FUP-1451): read the SHARED ledger.
#
# This block used to sum execution_result_*.json only — the executor envelopes — which is a
# different population from the one spend.json tracked (planner+consumer). Two budget checks
# guarding one run against two disjoint subsets of the same spend is not a cap; whichever
# subset happened to cross first decided the run's fate, and neither ever saw the whole
# figure. On factory_dryrun run 1 this check saw $72.35 and spend.json saw $90.04 while the
# run had actually spent $189.93.
#
# Reading the ledger makes both checks compare the same number. Fall back to the old
# envelope sum only when no ledger exists (a run started before this fix), so an in-flight
# legacy state dir still gets a cap rather than an unguarded zero.
if [[ -f "$STATE_DIR/cost_ledger.jsonl" ]]; then
  spent="$(awk -F'"cost_usd":' 'NF>1{ split($2,a,","); s+=a[1] } END{ printf "%.6f", s+0 }' "$STATE_DIR/cost_ledger.jsonl" 2>/dev/null || echo 0)"
else
  spent="0"
  if [[ -d "$STATE_DIR/iterations" ]]; then
    while IFS= read -r f; do
      c="$(jq -r '.total_cost_usd // 0' "$f" 2>/dev/null || echo 0)"
      spent="$(awk -v a="$spent" -v b="$c" 'BEGIN{printf "%.6f", a+b}')"
    done < <(find "$STATE_DIR/iterations" -maxdepth 2 -name 'execution_result_*.json')
  fi
fi
over="$(awk -v s="$spent" -v m="$TOKENS_USD_MAX" 'BEGIN{print (s>m)?1:0}')"
if [[ "$over" -eq 1 ]]; then
  # FUP-1484: name the basis and the override so the message cannot be read as the seed cap.
  echo "budget_check: tokens_usd ceiling exceeded (spent=$spent > max=$TOKENS_USD_MAX; basis=$([[ -f "$STATE_DIR/cost_ledger.jsonl" ]] && echo cost_ledger.jsonl || echo execution_result_fallback)$([[ -f "$STATE_DIR/budget_override.json" ]] && echo ', cap raised by budget_override.json' || echo ''))" >&2
  exit 1
fi
exit 0
