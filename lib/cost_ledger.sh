#!/usr/bin/env bash
# lib/cost_ledger.sh — the SINGLE cost basis for a run (FUP-1451).
#
# THE DEFECT THIS REPLACES. Before this file, four code paths each kept their own
# partial total of the same quantity, and every one of them was wrong:
#
#   spend.json                      planner + consumer only   ($90.04 on factory_dryrun run 1)
#   budget_check.sh                 executor envelopes only   ($72.35)
#   state_snapshot.tokens_usd_...   executor subset, written by the Consumer LLM   ($68.06)
#   truth (all four roles on disk)                            ($189.93)
#
# The counter under-reported by 2.11x because cost recording was OPT-IN PER CALL SITE:
# whoever added a new `claude -p` call simply did not add the jq/awk block that updates
# spend.json, and nothing ever noticed. plan_review's two call sites (and its 3x retry
# loop), the executor, the executor's --resume report recovery and the answerer were all
# invisible. This is not a rounding problem. The iteration-0011 Planner read the low
# number, concluded the budget was nearly exhausted, and escalated a gate_human on it.
#
# THE FIX HAS TWO HALVES, and the second is the one that keeps it fixed:
#   1. ledger_record() — every role records through here, so the accounting lives in ONE
#      place instead of N. Callers pass an envelope; extraction is not their business.
#   2. ledger_reconcile() — an INDEPENDENT oracle. It re-derives the total by walking the
#      CLI-written envelopes on disk (a basis the ledger never touches) and RAISEs when the
#      two disagree beyond tolerance. A wrapper alone would fail silently the next time
#      someone adds a call site that bypasses it; the reconciliation is what makes that
#      bypass LOUD. Half 1 fixes today's numbers, half 2 fixes tomorrow's.
#
# Ledger file:  $STATE_DIR/cost_ledger.jsonl   (append-only, one record per LLM call)
# Rollup file:  $STATE_DIR/spend.json          (.total_spend_usd — unchanged field name, so
#                                               supervisor/run_lifecycle.py, webui and
#                                               command_dispatch keep reading it and simply
#                                               start getting the true figure)
#
# Recording is idempotent: a record carries a dedup_key and re-recording the same key is a
# no-op. Re-running a hook against an existing envelope therefore cannot double-count.

# --- internal: atomic lock -------------------------------------------------------------
# The orchestrator and its hooks are separate PROCESSES writing one rollup file, so the
# read-modify-write of spend.json needs mutual exclusion. mkdir is atomic on every
# filesystem this runs on (incl. MSYS over a network share); flock is not always present.
_ledger_lock() {
  local _dir="$1/.cost_ledger.lock" _n=0
  while ! mkdir "$_dir" 2>/dev/null; do
    _n=$((_n + 1))
    # Stale-lock break: a killed orchestrator leaves the dir behind, and a lock nobody can
    # ever acquire would wedge every later call. 100 * 0.1s = 10s is far longer than any
    # legitimate holder (a jq + mv), so breaking after it is safe.
    if [[ "$_n" -gt 100 ]]; then rm -rf "$_dir" 2>/dev/null || true; mkdir "$_dir" 2>/dev/null || return 1; break; fi
    sleep 0.1 2>/dev/null || sleep 1
  done
  return 0
}
_ledger_unlock() { rmdir "$1/.cost_ledger.lock" 2>/dev/null || true; }

# --- ledger_record_cost <state_dir> <role> <usd> <dedup_key> ---------------------------
# Core primitive. Appends to the ledger and re-rolls spend.json. Never aborts the caller:
# every caller runs under `set -euo pipefail` and a cost-accounting hiccup must not kill a
# run that is otherwise fine (the reconciliation will catch what this drops).
ledger_record_cost() {
  local _sd="$1" _role="$2" _usd="$3" _key="$4"
  [[ -n "$_sd" && -d "$_sd" ]] || return 0
  [[ -n "$_usd" && "$_usd" != "null" ]] || _usd=0
  # Guard against a non-numeric envelope value reaching jq --argjson (which dies on it).
  awk -v v="$_usd" 'BEGIN{ exit !(v+0==v) }' 2>/dev/null || _usd=0

  local _ledger="$_sd/cost_ledger.jsonl" _spend="$_sd/spend.json"
  _ledger_lock "$_sd" || return 0

  # Idempotency: a fixed-string match on the dedup_key field. If the key is already on
  # record this call contributed nothing new, so neither the ledger nor the rollup moves.
  if [[ -f "$_ledger" ]] && grep -qF "\"dedup_key\":\"$_key\"" "$_ledger" 2>/dev/null; then
    _ledger_unlock "$_sd"; return 0
  fi

  printf '{"ts":"%s","role":"%s","cost_usd":%s,"dedup_key":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$_role" "$_usd" "$_key" >> "$_ledger" 2>/dev/null || true

  # Roll up FROM THE LEDGER, not by incrementing the previous rollup. Deriving the total
  # from the append-only record every time means spend.json cannot drift away from its own
  # evidence — a lost or duplicated increment self-corrects on the next call.
  local _total
  _total="$(awk -F'"cost_usd":' 'NF>1{ split($2,a,","); s+=a[1] } END{ printf "%.6f", s+0 }' "$_ledger" 2>/dev/null || echo 0)"
  printf '{\n  "total_spend_usd": %s\n}\n' "$_total" > "$_spend.tmp" 2>/dev/null \
    && mv -f "$_spend.tmp" "$_spend" 2>/dev/null || true

  _ledger_unlock "$_sd"
  return 0
}

# --- ledger_record <state_dir> <role> <envelope.json> ----------------------------------
# The form every call site should use: hand it the `claude --output-format json` envelope
# and the role name. Cost extraction, the 0-byte/unparseable case and dedup are handled
# here so no call site has to remember them.
ledger_record() {
  local _sd="$1" _role="$2" _file="$3"
  [[ -n "$_file" && -f "$_file" ]] || return 0
  local _cost _jq_rc=0
  _cost="$(jq -r '.total_cost_usd // 0' "$_file" 2>/dev/null)" || _jq_rc=$?
  # A SILENT ZERO is the exact failure mode this whole file exists to eliminate, so an
  # envelope that is present but unreadable must be loud rather than counted as free.
  # Observed for real: a path 267 characters long is past Windows MAX_PATH (260), so the
  # native jq.exe reported "No such file or directory" for a file `find` had just listed,
  # and the cost silently became 0. Anything that makes an envelope unparseable — long
  # path, truncated write, 0-byte file from a killed CLI — lands here.
  if [[ "$_jq_rc" -ne 0 || -z "$_cost" ]]; then
    echo "cost_ledger: WARNING — envelope exists but its cost could not be read: $_file (role=$_role). Recording 0; the reconciliation will flag the gap." >&2
    _cost=0
  fi
  [[ "$_cost" != "null" ]] || _cost=0
  # Key on role + filename + cost. Cost is in the key deliberately: if an envelope is
  # legitimately REWRITTEN by a retry with a different cost, that is a genuinely new call
  # and must count; an identical re-read of the same envelope must not.
  ledger_record_cost "$_sd" "$_role" "$_cost" "${_role}:$(basename "$_file"):${_cost}"
}

# --- ledger_total <state_dir> ----------------------------------------------------------
ledger_total() {
  local _sd="$1"
  [[ -f "$_sd/spend.json" ]] || { echo 0; return 0; }
  jq -r '.total_spend_usd // 0' "$_sd/spend.json" 2>/dev/null || echo 0
}

# --- ledger_disk_total <state_dir> -----------------------------------------------------
# THE INDEPENDENT ORACLE. Sums .total_cost_usd across every JSON envelope the Claude CLI
# wrote under iterations/. It shares no code, no file and no bookkeeping with the ledger —
# the CLI writes these envelopes whether or not anything records them — so agreement
# between the two is evidence, not tautology.
ledger_disk_total() {
  local _sd="$1"
  [[ -d "$_sd/iterations" ]] || { echo 0; return 0; }
  find "$_sd/iterations" -maxdepth 2 -name '*.json' -print0 2>/dev/null \
    | xargs -0 -r jq -r 'if type=="object" then (.total_cost_usd // empty) else empty end' 2>/dev/null \
    | awk '{s+=$1} END{printf "%.6f", s+0}'
}

# --- ledger_reconcile <state_dir> [tolerance_usd] --------------------------------------
# Exit 0 = bases agree. Exit 1 = they diverge -> RAISE.
# Tolerance defaults to $0.50: large enough to absorb float noise across ~50 calls, far
# smaller than the ~$100 divergence the defect produced, so it cannot mask a recurrence.
ledger_reconcile() {
  local _sd="$1" _tol="${2:-0.50}"
  local _led _disk _delta
  _led="$(ledger_total "$_sd")"
  _disk="$(ledger_disk_total "$_sd")"
  _delta="$(awk -v a="$_led" -v b="$_disk" 'BEGIN{d=a-b; if(d<0)d=-d; printf "%.6f", d}')"

  # Name the populations that neither basis can see, so a residual is a KNOWN residual
  # rather than a silent one. stop_check's audit-skill spawns and adversarial_verify do not
  # request --output-format json, so no envelope exists to sum; they are outside both bases
  # by construction and are reported, not hidden.
  local _uncounted=""
  [[ -f "$_sd/logs/report_recovery.log" ]] && _uncounted="report_recovery.log present"

  if awk -v d="$_delta" -v t="$_tol" 'BEGIN{ exit !(d > t) }'; then
    echo "cost_ledger: RECONCILIATION FAILED — ledger=$_led disk_envelopes=$_disk delta=$_delta (tolerance=$_tol)" >&2
    echo "cost_ledger: a divergence here means a \`claude\` call site is not recording through ledger_record()." >&2
    echo "cost_ledger: find it with: diff <(jq -r .dedup_key $_sd/cost_ledger.jsonl | sort) <(find $_sd/iterations -maxdepth 2 -name '*.json' | sort)" >&2
    [[ -n "$_uncounted" ]] && echo "cost_ledger: note — $_uncounted (recovery calls are recorded separately; check they are on the ledger)" >&2
    return 1
  fi
  echo "cost_ledger: reconciliation OK — ledger=$_led disk_envelopes=$_disk delta=$_delta (tolerance=$_tol)"
  return 0
}

# --- CLI mode --------------------------------------------------------------------------
# Sourced by the orchestrator and hooks; also runnable directly so the reconciliation can
# be executed against a finished run's state dir without starting an orchestrator:
#   lib/cost_ledger.sh reconcile <state_dir> [tolerance]
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  set -uo pipefail
  case "${1:-}" in
    record)    ledger_record "$2" "$3" "$4" ;;
    total)     ledger_total "$2" ;;
    disk)      ledger_disk_total "$2"; echo ;;
    reconcile) ledger_reconcile "$2" "${3:-0.50}" ;;
    *) echo "usage: cost_ledger.sh {record <state_dir> <role> <envelope.json>|total <state_dir>|disk <state_dir>|reconcile <state_dir> [tol]}" >&2; exit 64 ;;
  esac
fi
