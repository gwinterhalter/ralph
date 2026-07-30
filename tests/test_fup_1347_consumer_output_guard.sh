#!/usr/bin/env bash
# tests/test_fup_1347_consumer_output_guard.sh
#
# FUP-1347: the Consumer could silently produce NOTHING and the iteration closed nothing.
#
#   Observed 2026-07-29, Factory_Backlog iteration 0003: consumer.json was 0 bytes and
#   consumer.json.err was 0 bytes, and the last line of logs/events.jsonl was the consumer
#   `role_call` with NO terminal event after it. The iteration fixed its target and satisfied
#   its oracle and still closed nothing -- no registry row reached RESOLVED -- and no surface
#   recorded why. Two independent silent-death routes existed:
#     (1) the `claude` invocation in run_claude_json was bare under `set -euo pipefail`, so a
#         non-zero rc killed the orchestrator mid-function, before ANY recording could run
#         (the surviving 0-byte .err file is the proof: the `rm -f` below it never executed);
#     (2) even surviving that, `jq -r '.total_cost_usd // 0'` on a 0-byte file prints nothing
#         and exits 0, so `--argjson cc ""` aborted with a cryptic jq error instead.
#   Nothing in the tree ever READ consumer.json, so the envelope was write-only.
#
#   Fix: capture the claude rc (RUN_CLAUDE_LAST_RC), default the cost read, and gate both
#   Consumer call sites on require_role_json BEFORE the phase_complete liveness heartbeat.
#
# THE ASSERTION THAT MATTERS: the guard must be able to FAIL, and a genuine no-op must NOT.
#   A / B / C  -- function-level, exercised against the REAL function text extracted from
#                 orchestrator.sh (not a copy pasted into this test, which could drift).
#   D          -- negative control: a VALID envelope passes. Without D, a guard that
#                 rejected everything would score 100% on A-C.
#   E / F      -- integration: the real orchestrator at the real Consumer call site.
#                 E = 0-byte consumer.json -> rc 3, recorded iteration_failed, and NO
#                     phase_complete (an emitted heartbeat after a no-output Consumer is a
#                     liveness signal that lies).
#                 F = valid consumer.json -> the guard stays silent and the run proceeds.
#   G          -- both call sites (main loop + the SS6.3 resume leg) are guarded.
#
# Run: bash tests/test_fup_1347_consumer_output_guard.sh   (exit 0 = all PASS)

set -uo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RALPH_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
ORCH="$RALPH_ROOT/orchestrator.sh"
PASS=0; FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*" >&2; FAIL=$((FAIL+1)); }

# ---------------------------------------------------------------------------
# Extract the REAL require_role_json from orchestrator.sh.
# Extracted, never re-typed: a hand-copied function would let the test keep
# passing after the shipped one regressed, which is worse than no test.
# ---------------------------------------------------------------------------
HARNESS="$(mktemp -d)"
trap 'rm -rf "$HARNESS"' EXIT
awk '/^require_role_json\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$ORCH" \
  | tr -d '\r' > "$HARNESS/guard.sh"
if [[ ! -s "$HARNESS/guard.sh" ]] || ! grep -q 'output_invalid' "$HARNESS/guard.sh"; then
  echo "FAIL: could not extract require_role_json from $ORCH -- the harness is broken, not the code" >&2
  exit 1
fi
pass "[harness] extracted require_role_json ($(wc -l < "$HARNESS/guard.sh") lines) from the shipped orchestrator.sh"

# Stub only the COLLABORATORS (log / emit_event / dispatch_notification), never the
# function under test. emit_event appends to a real file so the recorded-failure
# assertions below read an artefact rather than trusting a call happened.
mk_stubs() {
  local sd="$1"
  mkdir -p "$sd/logs" "$sd/escalations"
  cat > "$HARNESS/stubs.sh" <<STUB
STATE_DIR="$sd"; SEED="$sd/seed.md"; touch "\$SEED"
EVENT_PROJECT_ID="t"; EVENT_SLUG="t"
log() { printf '%s\n' "\$*" >> "$sd/logs/orchestrator.log"; }
dispatch_notification() { printf '%s\n' "\$3" >> "$sd/logs/notifications.log"; }
# emit_event contract (lib/events.sh): <state_dir> <project_id> <slug> <iteration_index>
# <role> <event_type> [duration_ms] [subject_id] [subject_kind] [payload_json]
# so role=\$5, event_type=\$6, payload=\${10}. Getting this off by one made the stub
# mis-record the event and fail an assertion the shipped code satisfies -- fixed here
# rather than by weakening the assertion (the real emit_event proves it in [E]).
emit_event() { printf '{"role":"%s","event_type":"%s","payload":%s}\n' "\$5" "\$6" "\${10:-null}" >> "$sd/logs/events.jsonl"; }
STUB
}

# run_guard <state_dir> <out_file> <rc> -> echoes the guard's exit code
run_guard() {
  local sd="$1" out="$2" rc="${3:-0}"
  ( set -uo pipefail
    source "$HARNESS/stubs.sh"
    source "$HARNESS/guard.sh"
    require_role_json "$out" "consumer" "0007" "$rc"
  ) >>"$sd/guard.out" 2>>"$sd/guard.err"
  echo $?
}

recorded_failure() {   # a real iteration_failed event with the right reason
  local sd="$1"
  jq -e 'select(.event_type=="iteration_failed") | .payload.reason=="consumer_output_invalid"' \
     "$sd/logs/events.jsonl" >/dev/null 2>&1
}

# ---------- A: a ZERO-BYTE consumer.json is a hard failure ----------
A="$HARNESS/a"; mk_stubs "$A"
: > "$A/consumer.json"                      # exactly the observed 0-byte artefact
RC="$(run_guard "$A" "$A/consumer.json" 0)"
if [[ "$RC" == "3" ]]; then pass "[A] 0-byte consumer.json -> hard failure (exit 3), not a silent pass"
else fail "[A] expected exit 3, got $RC"; fi
grep -q 'ZERO-BYTE' "$A/guard.err" \
  && pass "[A] message names the 0-byte case and distinguishes it from ran-with-nothing-to-do" \
  || fail "[A] expected a ZERO-BYTE diagnostic, got: $(tail -1 "$A/guard.err")"
recorded_failure "$A" \
  && pass "[A] recorded iteration_failed(reason=consumer_output_invalid) -- the failure is WRITTEN DOWN" \
  || fail "[A] no iteration_failed event with reason=consumer_output_invalid"
[[ -s "$A/escalations/iteration_0007_failed.json" ]] \
  && jq -e '.reason=="consumer_output_invalid" and (.detail|length>0)' "$A/escalations/iteration_0007_failed.json" >/dev/null 2>&1 \
  && pass "[A] escalation artefact written with reason + detail" \
  || fail "[A] escalation file missing or malformed"

# ---------- B: an UNPARSEABLE consumer.json is a hard failure ----------
# The bare-jq trap: `jq '.x' </dev/null` prints nothing and exits 0, which is how a
# malformed envelope reads as a clean result (the FUP-0852 bug class). The guard uses jq -e.
B="$HARNESS/b"; mk_stubs "$B"
printf 'this is not json at all\n' > "$B/consumer.json"
RC="$(run_guard "$B" "$B/consumer.json" 0)"
[[ "$RC" == "3" ]] && pass "[B] unparseable consumer.json -> hard failure (exit 3)" \
                   || fail "[B] expected exit 3, got $RC"
grep -q 'unparseable' "$B/guard.err" && pass "[B] message names the unparseable case" \
                                     || fail "[B] no unparseable diagnostic"
# a JSON scalar is parseable but is not an envelope -- type=="object" is the real check
printf '42\n' > "$B/consumer.json"
RC="$(run_guard "$B" "$B/consumer.json" 0)"
[[ "$RC" == "3" ]] && pass "[B] a bare JSON scalar (42) is rejected -- parseable is not an envelope" \
                   || fail "[B] scalar 42 accepted (expected 3, got $RC)"

# ---------- C: a non-zero CLI rc, and an absent file, are hard failures ----------
C="$HARNESS/c"; mk_stubs "$C"
printf '{"result":"ok","total_cost_usd":0.1}\n' > "$C/consumer.json"   # good file, bad rc
RC="$(run_guard "$C" "$C/consumer.json" 143)"
[[ "$RC" == "3" ]] && pass "[C] rc=143 from the CLI -> hard failure even though the file parses" \
                   || fail "[C] expected exit 3 on rc=143, got $RC"
grep -q 'rc=143' "$C/guard.err" && pass "[C] message reports the actual rc (143)" \
                                || fail "[C] rc not surfaced in the message"
RC="$(run_guard "$C" "$C/absent.json" 0)"
[[ "$RC" == "3" ]] && pass "[C] absent consumer.json -> hard failure" \
                   || fail "[C] expected exit 3 for an absent file, got $RC"

# ---------- D: NEGATIVE CONTROL -- a valid envelope must PASS ----------
# Required, and it is the assertion that keeps A-C honest: a guard that rejected
# everything would pass A, B and C and be useless. It also pins the no-op rule --
# "correctly found nothing" is a success, so an envelope closing ZERO items passes.
D="$HARNESS/d"; mk_stubs "$D"
printf '{"result":"consumer ran; 0 items closed","total_cost_usd":0.02}\n' > "$D/consumer.json"
RC="$(run_guard "$D" "$D/consumer.json" 0)"
[[ "$RC" == "0" ]] && pass "[D] valid envelope -> exit 0 (the guard can PASS, not only fail)" \
                   || fail "[D] valid envelope rejected: expected 0, got $RC :: $(tail -1 "$D/guard.err")"
[[ ! -f "$D/logs/events.jsonl" ]] || ! recorded_failure "$D" \
  && pass "[D] no iteration_failed recorded for a healthy no-op Consumer (no crying wolf)" \
  || fail "[D] a valid no-op Consumer was recorded as a failure"
[[ ! -s "$D/escalations/iteration_0007_failed.json" ]] \
  && pass "[D] no escalation artefact for a healthy no-op Consumer" \
  || fail "[D] escalation written for a valid envelope"

# ---------------------------------------------------------------------------
# E / F -- integration: the REAL orchestrator, at the REAL Consumer call site.
# ---------------------------------------------------------------------------
build_sandbox() {
  local root="$1" consumer_mode="$2"
  mkdir -p "$root/bin" "$root/hooks" "$root/ws/state/commands" "$root/ralph"
  # mock claude: planner emits a plan; executor completes cleanly (so the Consumer is
  # REACHED -- a failing executor would skip it); consumer output varies per mode.
  cat > "$root/bin/claude" <<SHIM
#!/usr/bin/env bash
PROMPT="\$*"
if echo "\$PROMPT" | grep -qw -- '--print'; then
  cat > /dev/null
  echo '{"session_id":"mock","result":"done","total_cost_usd":0.0,"permission_denials":[],"terminal_reason":"completed"}'
  exit 0
fi
LAST_ARG="\${!#}"; ITER_DIR="\${LAST_ARG##* }"; [[ "\$ITER_DIR" != */iterations/* ]] && ITER_DIR="."
mkdir -p "\$ITER_DIR" 2>/dev/null || true; ITER_NUM="\$(basename "\$ITER_DIR")"
if echo "\$PROMPT" | grep -q '/rl-initiative-planner'; then
  printf -- '---\niteration_index: %s\nshape: noop\nmax_turns: 1\ntarget_item_id: ITEM-001\n---\n# plan\nFindings: 0 BLOCKER, 0 DRIFT\n' "\$ITER_NUM" > "\$ITER_DIR/session_plan_\${ITER_NUM}.md"
  printf -- '# report\n\n## Items closed\n\n- ITEM-001\n' > "\$ITER_DIR/execution_report_\${ITER_NUM}.md"
  echo '{"result":"mock planner","total_cost_usd":0.0}'
elif echo "\$PROMPT" | grep -q '/rl-iteration-consumer'; then
  case "$consumer_mode" in
    empty)   exit 0 ;;                                              # 0 bytes on stdout
    garbage) echo 'not json' ;;
    *)       echo '{"result":"consumer ok; 0 items closed","total_cost_usd":0.01}' ;;
  esac
else
  echo '{"session_id":"mock","result":"x","total_cost_usd":0.0,"permission_denials":[],"terminal_reason":"completed"}'
fi
SHIM
  chmod +x "$root/bin/claude"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$root/bin/win11toast"; chmod +x "$root/bin/win11toast"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$root/hooks/plan_review.sh"; chmod +x "$root/hooks/plan_review.sh"
  for h in stop_check.sh execute_with_gates.sh budget_check.sh; do cp "$RALPH_ROOT/hooks/$h" "$root/hooks/$h"; done
  printf '# reg\n\n| ID | Status | Title |\n|---|---|---|\n| ITEM-001 | open | an item |\n' > "$root/ws/registry.md"
  cat > "$root/seed.md" <<EOF
---
seed_schema_version: 1.3
initiative: { slug: t1347, title: t, owner: t, description: t }
workspace_root: "$root/ws"
state_dir_relative: "state/"
work_registry: "registry.md"
read_only_paths: []
context_documents: []
target_order: [ITEM-001]
session_shape_catalog: [ { name: noop, template_pointer: "prompt_key:rl.session_shape.noop" } ]
verification_bindings: []
verification_policy: inline_per_session_plan
mcp_servers: []
budget: { tokens_usd: 100.0, iterations_max: 1 }
permission_posture: "--permission-mode auto"
completion_predicate:
  - name: registry_drained
    check_kind: registry_zero_open
    params: { path: "registry.md", filter: "Status != closed" }
---
EOF
  echo '{"total_spend_usd":0.0}' > "$root/ws/state/spend.json"
  ln -s "$RALPH_ROOT/lib" "$root/ralph/lib"; ln -s "$RALPH_ROOT/schemas" "$root/ralph/schemas"
  ln -s "$root/hooks" "$root/ralph/hooks"
  cp "$ORCH" "$root/ralph/orchestrator.sh"
}
run_orch() {
  local root="$1"
  PATH="$root/bin:$PATH" CLAUDE_SKILLS_DIR="$RALPH_ROOT" \
    bash -c "unset MSYS_NO_PATHCONV; exec bash \"$root/ralph/orchestrator.sh\" \"$root/seed.md\"" \
    > "$root/orch.out" 2> "$root/orch.err"
  echo $?
}
consumer_event() {   # consumer_event <root> <event_type> -> 0 if present
  jq -e --arg t "$2" 'select(.role=="consumer" and .event_type==$t)' \
     "$1/ws/state/logs/events.jsonl" >/dev/null 2>&1
}

# ---------- E: 0-byte consumer.json at the real call site ----------
E="$(mktemp -d)"; build_sandbox "$E" empty
RC="$(run_orch "$E")"
if [[ "$RC" == "3" ]] && grep -q 'consumer_output_invalid' "$E/orch.err"; then
  pass "[E] real orchestrator: 0-byte consumer.json -> HALT rc 3 with consumer_output_invalid"
else
  fail "[E] expected rc3 + consumer_output_invalid, got rc=$RC :: $(tail -3 "$E/orch.err")"
fi
consumer_event "$E" role_call \
  && pass "[E] consumer role_call was emitted (the run really did reach the Consumer)" \
  || fail "[E] never reached the Consumer -- test is not exercising the guard :: $(tail -3 "$E/orch.err")"
jq -e 'select(.event_type=="iteration_failed") | .payload.reason=="consumer_output_invalid"' \
   "$E/ws/state/logs/events.jsonl" >/dev/null 2>&1 \
  && pass "[E] iteration_failed(consumer_output_invalid) recorded in events.jsonl" \
  || fail "[E] failure not recorded in events.jsonl"
# The alert-design assertion: the liveness heartbeat must NOT fire on this path.
consumer_event "$E" phase_complete \
  && fail "[E] phase_complete emitted after a no-output Consumer -- the liveness signal LIES" \
  || pass "[E] no phase_complete after a no-output Consumer -- silence means exactly one thing"

# ---------- F: NEGATIVE CONTROL at the real call site ----------
F="$(mktemp -d)"; build_sandbox "$F" ok
RC="$(run_orch "$F")"
grep -q 'consumer_output_invalid' "$F/orch.err" \
  && fail "[F] a VALID consumer envelope was rejected -- the guard is over-firing :: $(tail -3 "$F/orch.err")" \
  || pass "[F] real orchestrator: valid consumer.json -> guard silent (rc=$RC, no false positive)"
consumer_event "$F" phase_complete \
  && pass "[F] phase_complete heartbeat emitted for a healthy Consumer (liveness intact)" \
  || fail "[F] no phase_complete for a healthy Consumer :: $(tail -3 "$F/orch.err")"
[[ -s "$F/ws/state/iterations/0001/consumer.json" ]] \
  && pass "[F] consumer.json is non-empty on the healthy path" \
  || fail "[F] consumer.json empty on the healthy path"

# ---------- G: BOTH call sites are guarded ----------
GUARDED="$(grep -cE '^[[:space:]]*require_role_json "' "$ORCH")"
[[ "$GUARDED" == "2" ]] \
  && pass "[G] both Consumer call sites guarded (main loop + SS6.3 resume leg): $GUARDED" \
  || fail "[G] expected 2 require_role_json call sites, found $GUARDED"
grep -qE 'RUN_CLAUDE_LAST_RC=0' "$ORCH" \
  && pass "[G] RUN_CLAUDE_LAST_RC initialised (set -u safe)" \
  || fail "[G] RUN_CLAUDE_LAST_RC not initialised -- set -u would abort"

rm -rf "$E" "$F"
echo "-----"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
