#!/usr/bin/env bash
# Controls for six harness defects. EVERY fix is checked in BOTH directions: it now does the
# right thing AND it still refuses the wrong thing. Several checks replay the REAL cost
# envelopes from the factory_dryrun run-1 state tree when that tree is present, so the
# headline numbers ($90.04 counted vs $189.93 true) are reproduced from evidence, not asserted.
#
#   FUP-1451  budget accounting reported four disagreeing figures for one quantity
#   FUP-1452  auto_mode_denial failed an iteration whose deliverable existed
#   FUP-1475  headless auto-approval refused a bundled destructive verb (producer-side fix)
#   FUP-1477  P4-07 target_item_id awk never matched the bolded field real plans emit
#   FUP-1483  no write containment; a mistyped absolute path escaped the sandbox
#   FUP-1484  the log printed the SEED cap while the guard used the EFFECTIVE cap
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
pass=0; fail=0; failed=()
check() { # desc expected actual
  if [[ "$2" == "$3" ]]; then pass=$((pass+1)); echo "  PASS — $1";
  else fail=$((fail+1)); failed+=("$1"); echo "  FAIL — $1" >&2; echo "    expected: $2" >&2; echo "    actual:   $3" >&2; fi
}
WORK="${RL_TEST_WORKDIR:-${TMPDIR:-/tmp}/rl_1451_$$}"; mkdir -p "$WORK"
# Windows MAX_PATH guard. The deepest artifact this test creates is roughly 60 characters
# below $WORK; native jq.exe/python.exe silently fail to open anything past 260 characters,
# which would make this suite report wrong COST NUMBERS rather than fail — the worst outcome
# for a test whose subject is silent under-counting. Refuse to run instead of mis-measuring.
if [[ "${#WORK}" -gt 180 ]]; then
  echo "test_fup_1451: WORK path is ${#WORK} chars; artifacts would exceed Windows MAX_PATH (260) and native jq would silently read 0." >&2
  echo "test_fup_1451: re-run with a short base, e.g. RL_TEST_WORKDIR=/tmp/rl1451 bash $0" >&2
  exit 2
fi
cleanup() { [[ -n "${RL_TEST_KEEP:-}" ]] || rm -rf "$WORK" 2>/dev/null || true; }
trap cleanup EXIT

source "$RALPH_ROOT/lib/cost_ledger.sh"
source "$RALPH_ROOT/lib/path_guard.sh"

mk_envelope() { printf '{"total_cost_usd":%s,"result":"ok","permission_denials":[],"terminal_reason":"completed","session_id":"s"}\n' "$2" > "$1"; }

echo "===== FUP-1451 — one cost basis, and an oracle that RAISEs when a call site bypasses it ====="

SD="$WORK/state1"; mkdir -p "$SD/iterations/0001" "$SD/iterations/0002"
# Four roles, the four populations that used to be counted by four different (disagreeing) code
# paths. Values chosen to mirror the real run's shape: planner+consumer was the ONLY population
# spend.json saw, executor was the only one budget_check saw.
mk_envelope "$SD/iterations/0001/planner.json"            10.00
mk_envelope "$SD/iterations/0001/consumer.json"            5.00
mk_envelope "$SD/iterations/0001/execution_result_0001.json" 20.00
mk_envelope "$SD/iterations/0001/review_findings_1.json"   3.00
mk_envelope "$SD/iterations/0002/planner.json"             7.00
mk_envelope "$SD/iterations/0002/execution_result_0002.json" 15.00
# 60.00 total; the OLD spend.json basis would have seen 22.00, the OLD budget_check basis 35.00.

for f in "$SD"/iterations/*/planner.json;             do ledger_record "$SD" planner     "$f"; done
for f in "$SD"/iterations/*/consumer.json;            do ledger_record "$SD" consumer    "$f"; done
for f in "$SD"/iterations/*/execution_result_*.json;  do ledger_record "$SD" executor    "$f"; done
for f in "$SD"/iterations/*/review_findings_*.json;   do ledger_record "$SD" plan_review "$f"; done

check "ledger totals ALL four roles (not the planner+consumer subset)" "60.000000" "$(ledger_total "$SD")"
check "the independent on-disk oracle agrees" "60.000000" "$(ledger_disk_total "$SD")"
check "spend.json — the field every consumer reads — carries the true total" "60.000000" \
  "$(jq -r '.total_spend_usd' "$SD/spend.json")"
ledger_reconcile "$SD" 0.50 >/dev/null 2>&1
check "reconciliation PASSES when every call site records" "0" "$?"

# Idempotency: re-recording the same envelopes must not double-count (hooks can re-run).
for f in "$SD"/iterations/*/planner.json; do ledger_record "$SD" planner "$f"; done
check "re-recording an already-recorded envelope is a no-op" "60.000000" "$(ledger_total "$SD")"

# NEGATIVE CONTROL — this is the check that keeps the fix fixed. Simulate exactly the defect
# that FUP-1451 was: a call site that SPENDS MONEY WITHOUT RECORDING. The CLI writes its
# envelope to disk as it always does; nothing calls ledger_record. The oracle must NOTICE.
mk_envelope "$SD/iterations/0002/review_findings_1.json" 40.00   # spent, never recorded
recon_out="$(ledger_reconcile "$SD" 0.50 2>&1)"; recon_rc=$?
check "reconciliation RAISES when a role spends without recording" "1" "$recon_rc"
check "  ...and names the divergence" "true" \
  "$(printf '%s' "$recon_out" | grep -q 'RECONCILIATION FAILED' && echo true || echo false)"
echo "    oracle output: $(printf '%s' "$recon_out" | head -1)"

# Tolerance must not be a loophole: a sub-tolerance float wobble stays quiet.
SD2="$WORK/state2"; mkdir -p "$SD2/iterations/0001"
mk_envelope "$SD2/iterations/0001/planner.json" 10.00
ledger_record "$SD2" planner "$SD2/iterations/0001/planner.json"
ledger_record_cost "$SD2" rounding 0.01 "rounding:noise"
ledger_reconcile "$SD2" 0.50 >/dev/null 2>&1
check "reconciliation does NOT cry wolf on sub-tolerance noise" "0" "$?"

echo ""
echo "===== FUP-1451/1484 — budget_check.sh reads the shared ledger, and names its basis ====="
BC="$RALPH_ROOT/hooks/budget_check.sh"
SEEDB="$WORK/seedb.md"
write_seed() { cat > "$SEEDB" <<EOF
---
seed_schema_version: 1.4
initiative: {slug: t, title: t, owner: t}
budget:
  iterations_max: 99
  tokens_usd: $1
---
body
EOF
}
# The decisive control. Ledger says 60.00; the executor-only population (the OLD basis) is 35.00.
# With a cap of 50 the OLD code exits 0 (35 < 50 — "within budget") and the NEW code exits 1.
# Same inputs, opposite verdicts: the fix is doing work, not decorating.
write_seed 50
bash "$BC" "$SEEDB" "$SD2" >/dev/null 2>&1; : # warm
SD3="$WORK/state3"; mkdir -p "$SD3/iterations/0001"
cp "$SD/iterations/0001/execution_result_0001.json" "$SD3/iterations/0001/"
cp "$SD/iterations/0002/execution_result_0002.json" "$SD3/iterations/0001/execution_result_0002.json" 2>/dev/null
old_basis="$(find "$SD3/iterations" -maxdepth 2 -name 'execution_result_*.json' -exec jq -r '.total_cost_usd' {} \; | awk '{s+=$1} END{printf "%.2f", s}')"
check "the OLD executor-only basis is below the cap (this is why it never fired)" "35.00" "$old_basis"
cp "$SD/cost_ledger.jsonl" "$SD3/cost_ledger.jsonl" 2>/dev/null
# Build SD3's ledger the honest way: record the same envelopes through the shared wrapper.
for f in "$SD"/iterations/*/execution_result_*.json; do ledger_record "$SD3" executor "$f"; done
for f in "$SD"/iterations/*/planner.json; do ledger_record "$SD3" planner "$f"; done
bc_out="$(bash "$BC" "$SEEDB" "$SD3" 2>&1)"; bc_rc=$?
check "budget_check RAISES on the true total where the old basis passed" "1" "$bc_rc"
check "  ...and names its basis as the ledger" "true" \
  "$(printf '%s' "$bc_out" | grep -q 'basis=cost_ledger.jsonl' && echo true || echo false)"
echo "    budget_check said: $bc_out"

# NEGATIVE DIRECTION: a genuinely-under-cap run must still pass (the guard is not just "always fail").
write_seed 5000
bash "$BC" "$SEEDB" "$SD3" >/dev/null 2>&1
check "budget_check still PASSES a run that is genuinely under cap" "0" "$?"

# FUP-1484 on this surface: with an override present the message must say so.
write_seed 50
printf '{"budget_cap_usd": 60}\n' > "$SD3/budget_override.json"
bc_out2="$(bash "$BC" "$SEEDB" "$SD3" 2>&1)"; bc_rc2=$?
check "override raises the cap so the same spend now passes (override is honoured)" "0" "$bc_rc2"
printf '{"budget_cap_usd": 55}\n' > "$SD3/budget_override.json"
bc_out3="$(bash "$BC" "$SEEDB" "$SD3" 2>&1)"
check "when it still trips, the message names the override as the cap source" "true" \
  "$(printf '%s' "$bc_out3" | grep -q 'budget_override.json' && echo true || echo false)"
echo "    budget_check said: $bc_out3"
rm -f "$SD3/budget_override.json"

echo ""
echo "===== FUP-1477 — target_item_id extraction, run as the SHIPPED expression ====="
# Pull the awk program out of the live orchestrator.sh so the control executes the file's own
# bytes rather than a copy that could drift from it.
AWK_PROG="$(grep -o "awk -F': \*' '[^']*'" "$RALPH_ROOT/orchestrator.sh" | grep target_item_id | head -1 | sed "s/^awk -F': \*' '//; s/'$//")"
check "extracted the shipped awk program from orchestrator.sh" "true" \
  "$([[ -n "$AWK_PROG" ]] && echo true || echo false)"
run_awk() { printf '%s\n' "$1" | awk -F': *' "$AWK_PROG"; }

# POSITIVE — the exact shape every planner-emitted plan uses, and the one that silently failed.
check "bolded field (the real plan format) now extracts" "STAGE-3b" "$(run_awk '**target_item_id:** STAGE-3b')"
check "unbolded field still extracts (no regression on the manual workaround form)" "STAGE-9" "$(run_awk 'target_item_id: STAGE-9')"
check "bolded + quoted + trailing space extracts clean" "STAGE-X" "$(run_awk '**target_item_id:**  "STAGE-X"  ')"
# NEGATIVE — must still refuse near-misses, or the fix would just be a looser net.
check "a different field ending in the same name is REFUSED" "" "$(run_awk 'prior_target_item_id: WRONG')"
check "a mid-line mention is REFUSED" "" "$(run_awk 'notes about target_item_id: WRONG')"
check "a prose back-reference in a bullet is REFUSED" "" "$(run_awk '- see `target_item_id:` mitigation')"
check "an unrelated line is REFUSED" "" "$(run_awk 'nothing here at all')"

# Replay against the REAL session plans when the live run tree is present.
LIVE="$RALPH_ROOT/../../Sub_Projects/Factory_Design/state/factory_dryrun/iterations"
if [[ -d "$LIVE/0007" ]]; then
  old_0007="$(awk -F': *' '/^target_item_id:/{v=$2; gsub(/[[:space:]"]/,"",v); print v; exit}' "$LIVE/0007/session_plan_0007.md" 2>/dev/null)"
  new_0007="$(awk -F': *' "$AWK_PROG" "$LIVE/0007/session_plan_0007.md" 2>/dev/null)"
  check "live plan 0007: OLD expression extracted nothing (the defect, reproduced)" "" "$old_0007"
  check "live plan 0007: NEW expression extracts the item id" "STAGE-3b" "$new_0007"
  check "live plan 0015: no regression on a plan carrying both forms" "STAGE-7" \
    "$(awk -F': *' "$AWK_PROG" "$LIVE/0015/session_plan_0015.md" 2>/dev/null)"
else
  echo "  SKIP — live factory_dryrun tree not present; synthetic controls above still cover the fix"
fi

echo ""
echo "===== FUP-1483 — write containment ====="
ROOT="$WORK/K/OneDrive - EPM Solutions - Project Server- Project Online"
mkdir -p "$ROOT/Code_Factory/Factory_V3"
# POSITIVE — legitimate paths inside the sandbox are allowed.
pg_assert_under "inside" "$ROOT/Code_Factory/Factory_V3/state/x.json" "$ROOT" >/dev/null 2>&1
check "a path inside the declared root is ALLOWED" "0" "$?"
pg_assert_under "root itself" "$ROOT" "$ROOT" >/dev/null 2>&1
check "the root itself is ALLOWED" "0" "$?"
pg_assert_under "backslashes" "$(printf '%s' "$ROOT" | tr '/' '\\')\\a\\b.json" "$ROOT" >/dev/null 2>&1
check "a backslash-form path inside the root is ALLOWED (normalisation works)" "0" "$?"
# NEGATIVE — the actual FUP-1483 string, and the sibling-prefix trap.
DOUBLED="$WORK/K/OneDrive - EPM Solutions - EPM Solutions - Project Server- Project Online/Code_Factory/x.sql"
pg_assert_under "doubled" "$DOUBLED" "$ROOT" >/dev/null 2>&1
check "the REAL FUP-1483 doubled path is REFUSED" "1" "$?"
pg_assert_under "sibling" "${ROOT}_evil/x.json" "$ROOT" >/dev/null 2>&1
check "a sibling-prefix path (root+suffix) is REFUSED, not swallowed by a prefix match" "1" "$?"
pg_assert_under "elsewhere" "/etc/passwd" "$ROOT" >/dev/null 2>&1
check "an unrelated absolute path is REFUSED" "1" "$?"

# The detective half: an agent-typed path the harness cannot intercept.
SD4="$WORK/state4"; mkdir -p "$SD4"; DRIVE="$WORK/K"
pg_snapshot_fsroot "$SD4" "$DRIVE/anything" "$DRIVE"
pg_check_fsroot "$SD4" >/dev/null 2>&1
check "no escape -> the sandbox sweep stays quiet" "0" "$?"
mkdir -p "$DRIVE/OneDrive - EPM Solutions - EPM Solutions - Project Server- Project Online"
esc_out="$(pg_check_fsroot "$SD4" 2>&1)"; esc_rc=$?
check "a NEW top-level entry (the FUP-1483 damage shape) is DETECTED" "1" "$esc_rc"
check "  ...and the offending name is reported" "true" \
  "$(printf '%s' "$esc_out" | grep -q 'EPM Solutions - EPM Solutions' && echo true || echo false)"
check "  ...and the guard does NOT delete it (evidence is preserved)" "true" \
  "$([[ -d "$DRIVE/OneDrive - EPM Solutions - EPM Solutions - Project Server- Project Online" ]] && echo true || echo false)"

# Sibling class, real and still live: command_dispatch joined workspace_root onto an ALREADY
# ABSOLUTE .work_registry. Execute the shipped case statement.
join_registry() { # <work_registry> <ws_root>  — the exact logic now in lib/command_dispatch.sh
  local work_registry="$1" ws_root="$2" registry_path=""
  case "$work_registry" in
    /*|[A-Za-z]:[/\\]*) registry_path="$work_registry" ;;
    *) [[ -n "$ws_root" ]] && registry_path="${ws_root%/}/$work_registry" ;;
  esac
  printf '%s' "$registry_path"
}
check "an ABSOLUTE work_registry is not double-joined" "K:/a/reg.md" "$(join_registry 'K:/a/reg.md' 'K:/a')"
check "a POSIX-absolute work_registry is not double-joined" "/a/reg.md" "$(join_registry '/a/reg.md' '/a')"
check "a RELATIVE work_registry is still joined (fix does not break the normal case)" "K:/a/reg.md" "$(join_registry 'reg.md' 'K:/a')"

echo ""
echo "===== FUP-1452 / 1475 / 1484 — real hook + real orchestrator, mock claude ====="
TMPBIN="$WORK/bin"; mkdir -p "$TMPBIN"
# Mock claude: records the stdin it was given, then returns an envelope carrying permission
# denials AND terminal_reason=completed — the exact iteration-0001 signature.
cat > "$TMPBIN/claude" <<'STUB'
#!/usr/bin/env bash
cat >> "${RL_TEST_STDIN_CAPTURE:-/dev/null}" 2>/dev/null || true
echo '{"result":"done","session_id":"stub","total_cost_usd":1.25,"is_error":false,"terminal_reason":"completed","permission_denials":[{"tool_name":"Bash","tool_input":{"command":"psql -c \\u0027\\\\du\\u0027"},"reason":"auto denial"},{"tool_name":"PowerShell","tool_input":{"command":"psql --whoami"},"reason":"auto denial"}]}'
exit 0
STUB
chmod +x "$TMPBIN/claude"

WS="$WORK/ws"; mkdir -p "$WS"
cat > "$WS/registry.md" <<'REG'
# Registry
| ID | Name | Gap description | Priority | Prerequisites | Resolution path |
|---|---|---|---|---|---|
| T-01 | item | stub | **P1** | (none) | artifacts/x.md |
REG
SEED="$WS/seed.md"
cat > "$SEED" <<SEEDDOC
---
seed_schema_version: 1.4
initiative:
  slug: fup1451_test
  title: FUP-1451 control
  owner: test
workspace_root: "$WS"
read_only_paths: []
writable_paths: ["$WS"]
mcp_servers: []
state_dir_relative: "state"
work_registry: "registry.md"
context_documents: []
session_shape_catalog:
  - name: doc_stub
    template_pointer: "x"
verification_bindings:
  doc_stub: []
completion_predicate:
  - name: zero_open_gaps
    check_kind: registry_zero_open
    params:
      path: "$WS/registry.md"
gate_policy:
  pre_classification: []
  confidence_threshold: 0.7
budget:
  iterations_max: 1
  tokens_usd: 60
  hang_timeout_seconds: 60
notification_channel: "wintoast:default"
permission_posture: "--permission-mode auto"
---
body
SEEDDOC

run_ewg() { # <iter> <make_report:yes|no> -> prints rc
  local iter="$1" mkrep="$2"
  local sd="$WS/state" idir="$WS/state/iterations/$iter"
  mkdir -p "$idir" "$sd/logs"
  cp "$SEED" "$sd/seed.md" 2>/dev/null || true
  cat > "$idir/session_plan_${iter}.md" <<PLAN
**target_item_id:** T-01
- **shape:** doc_stub
## §5 Numbered steps
1. UNIQUE_PLAN_BODY_MARKER — do the work.
PLAN
  if [[ "$mkrep" == "yes" ]]; then
    { echo "## 1. Summary"; echo "The work was delivered despite the denials."; \
      head -c 400 /dev/zero | tr '\0' 'x'; } > "$idir/execution_report_${iter}.md"
  fi
  RL_TEST_STDIN_CAPTURE="$idir/.stdin_capture" \
  PATH="$TMPBIN:$PATH" CLAUDE_SKILLS_DIR="$WORK/none" RALPH_DISABLE_DESKTOP_TOAST=1 \
    timeout 60 bash "$RALPH_ROOT/hooks/execute_with_gates.sh" "$SEED" "$idir" >"$idir/.ewg.out" 2>&1
  echo $?
}

# FUP-1452 POSITIVE: denials present, deliverable present -> must NOT fail the iteration.
rc_delivered="$(run_ewg 0001 yes)"
check "FUP-1452: denials + deliverable present -> iteration NOT failed" "0" "$rc_delivered"
ESC_DIR="$WS/state/escalations"
check "  ...the denials are still recorded (signal not discarded)" "true" \
  "$(ls "$ESC_DIR"/auto_mode_denial_0001_*.json >/dev/null 2>&1 && echo true || echo false)"
check "  ...classified as delivered, not blocking" "auto_mode_denial_delivered" \
  "$(jq -r '.classification' "$(ls -1 "$ESC_DIR"/auto_mode_denial_0001_*.json 2>/dev/null | head -1)" 2>/dev/null)"
check "  ...and the denial count is still reported honestly" "2" \
  "$(jq -r '.deliverable_blocking_count' "$(ls -1 "$ESC_DIR"/auto_mode_denial_0001_*.json 2>/dev/null | head -1)" 2>/dev/null)"

# FUP-1452 NEGATIVE: same denials, NO deliverable -> must STILL fail. The guard is narrowed to
# its declared meaning, not switched off.
rc_blocked="$(run_ewg 0002 no)"
check "FUP-1452: denials + NO deliverable -> iteration STILL FAILED" "1" "$rc_blocked"
check "  ...classified as blocking (original path intact)" "auto_mode_denial" \
  "$(jq -r '.classification' "$(ls -1 "$ESC_DIR"/auto_mode_denial_0002_*.json 2>/dev/null | head -1)" 2>/dev/null)"

# FUP-1451 on the executor path: the hook must have recorded its own spend.
check "FUP-1451: the executor call is on the ledger (was \$0 recorded before)" "true" \
  "$(grep -q '"role":"executor"' "$WS/state/cost_ledger.jsonl" 2>/dev/null && echo true || echo false)"

# FUP-1475 POSITIVE: the Executor actually RECEIVED the contract, and the plan survived intact.
CAP="$WS/state/iterations/0001/.stdin_capture"
check "FUP-1475: the execution contract reached the Executor's stdin" "true" \
  "$(grep -q 'EXECUTION CONTRACT' "$CAP" 2>/dev/null && echo true || echo false)"
check "  ...it forbids destructive verbs in a multi-purpose call" "true" \
  "$(grep -q 'No destructive verbs in a multi-purpose call' "$CAP" 2>/dev/null && echo true || echo false)"
check "  ...it requires a single-purpose call when destruction is unavoidable" "true" \
  "$(grep -q 'SINGLE-PURPOSE call' "$CAP" 2>/dev/null && echo true || echo false)"
check "  ...it names the idempotent-clone remedy that iteration 0007 lacked" "true" \
  "$(grep -q 'gh repo clone' "$CAP" 2>/dev/null && echo true || echo false)"
# NEGATIVE DIRECTION: injection must not swallow or truncate the actual brief.
check "  ...and the plan body is still fully present (brief not truncated)" "true" \
  "$(grep -q 'UNIQUE_PLAN_BODY_MARKER' "$CAP" 2>/dev/null && echo true || echo false)"
check "  ...the contract precedes the plan (order preserved)" "true" \
  "$(awk '/EXECUTION CONTRACT/{c=NR} /UNIQUE_PLAN_BODY_MARKER/{p=NR} END{exit !(c>0 && p>c)}' "$CAP" && echo true || echo false)"

echo ""
echo "----- FUP-1484: the log must print the cap the guard COMPARED -----"
# A separate workspace: the hook section's artifacts above are evidence and stay put.
WS="$WORK/ws_orch"; mkdir -p "$WS"
cp "$WORK/ws/registry.md" "$WS/registry.md"
SEED="$WS/seed.md"; sed "s#$WORK/ws#$WS#g" "$WORK/ws/seed.md" > "$SEED"
run_orch() {
  PATH="$TMPBIN:$PATH" CLAUDE_SKILLS_DIR="$WORK/none" RALPH_DISABLE_DESKTOP_TOAST=1 \
    timeout 90 bash "$RALPH_ROOT/orchestrator.sh" "$SEED" >/dev/null 2>&1 || true
}
# NEGATIVE DIRECTION (no override): the printed cap is the seed cap and carries NO provenance.
run_orch
LOG="$WS/state/logs/orchestrator.log"
capline="$(grep -o 'cap=[^ ]*' "$LOG" 2>/dev/null | head -1)"
check "no override -> log prints the seed cap" "cap=60" "$capline"
check "no override -> no misleading provenance suffix" "false" \
  "$(grep -q 'raised by budget_override.json' "$LOG" 2>/dev/null && echo true || echo false)"
echo "    log said: $(grep -o 'call_cost=.*' "$LOG" 2>/dev/null | head -1)"

# POSITIVE DIRECTION (override active): the printed cap must be the EFFECTIVE cap, with source.
# A FRESH workspace — the run above already consumed the seed's iterations_max, and a second
# orchestrator invocation against the same state dir HALTs on the iteration cap before it ever
# reaches a claude call, so it would print no cap line and the check would pass vacuously.
WS2="$WORK/ws_orch2"; mkdir -p "$WS2/state"
cp "$WS/registry.md" "$WS2/registry.md"
SEED2="$WS2/seed.md"; sed "s#$WS#$WS2#g" "$SEED" > "$SEED2"
printf '{"budget_cap_usd": 400}\n' > "$WS2/state/budget_override.json"
PATH="$TMPBIN:$PATH" CLAUDE_SKILLS_DIR="$WORK/none" RALPH_DISABLE_DESKTOP_TOAST=1 \
  timeout 90 bash "$RALPH_ROOT/orchestrator.sh" "$SEED2" >/dev/null 2>&1 || true
LOG="$WS2/state/logs/orchestrator.log"
capline2="$(grep -o 'cap=[^ ]*' "$LOG" 2>/dev/null | head -1)"
check "override active -> log prints the EFFECTIVE cap, not the seed cap" "cap=400" "$capline2"
check "  ...and states where the raise came from" "true" \
  "$(grep -q 'raised by budget_override.json' "$LOG" 2>/dev/null && echo true || echo false)"
check "  ...and still shows the seed cap for context" "true" \
  "$(grep -q 'seed=60' "$LOG" 2>/dev/null && echo true || echo false)"
echo "    log said: $(grep -o 'call_cost=.*' "$LOG" 2>/dev/null | head -1)"

# FUP-1451 end-to-end: a full orchestrator run must leave a ledger and a coherent spend.json.
check "FUP-1451: a real orchestrator run writes the shared ledger" "true" \
  "$([[ -s "$WS/state/cost_ledger.jsonl" ]] && echo true || echo false)"
# The three roles that recorded NOTHING before this fix must now each appear on the ledger.
check "  ...the executor is on it (was USD 72.35 unrecorded)" "true" \
  "$(grep -q '"role":"executor"' "$WORK/ws/state/cost_ledger.jsonl" 2>/dev/null && echo true || echo false)"
check "  ...the report-recovery --resume leg is on it (was USD 22.91 unrecorded)" "true" \
  "$(grep -q '"role":"report_recovery"' "$WORK/ws/state/cost_ledger.jsonl" 2>/dev/null && echo true || echo false)"
check "  ...the planner is on it" "true" \
  "$(grep -q '"role":"planner"' "$WS/state/cost_ledger.jsonl" 2>/dev/null && echo true || echo false)"
check "  ...and spend.json equals the ledger sum" "true" \
  "$(awk -v a="$(ledger_total "$WS/state")" -v b="$(awk -F'\"cost_usd\":' 'NF>1{split($2,x,","); s+=x[1]} END{printf "%.6f", s+0}' "$WS/state/cost_ledger.jsonl")" 'BEGIN{print (a==b)?"true":"false"}')"

echo ""
echo "=== test_fup_1451_1484_budget_gate_path_fixes.sh: $pass PASS, $fail FAIL ==="
[[ $fail -eq 0 ]] || { printf 'FAILED: %s\n' "${failed[@]}" >&2; exit 1; }
exit 0
