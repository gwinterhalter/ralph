#!/usr/bin/env bash
# tests/test_fup_0848_stop_check_multitable_register.sh
# FUP-0848: stop_check.sh registry_zero_open generic "<column> != <value>" evaluator must be
# TABLE-AWARE. The prior code assumed the work table was the FIRST markdown table in the file
# and counted cells across ALL subsequent | rows. That HALTed (exit 3) on the ol-build register,
# whose first table is a Preconditions table (no Status column), and would also have miscounted a
# trailing Change-History table. This test drives the REAL hooks/stop_check.sh against synthetic
# registers covering: multi-table Status (open + all-closed), Priority fallback (FUP-0829), and
# the genuine no-column HALT.
#
# Exit contract under test (stop_check.sh header): 0 = all predicates pass (complete);
# 1 = at least one failed (continue); >=3 = malformed/error (HALT).
#
# Run: bash tests/test_fup_0848_stop_check_multitable_register.sh   (exit 0 = all PASS)

set -uo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RALPH_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

PASS=0; FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*" >&2; FAIL=$((FAIL+1)); }

# Build a seed pointing registry_zero_open at an absolute register path + given filter.
# pred_name 'registry_drained' is NOT a special-cased name (zero_open_gaps /
# every_closure_cites_iteration), so it routes to the generic <column> != <value> evaluator.
make_seed() {
  local seed="$1" reg="$2" filt="$3"
  cat > "$seed" <<EOF
---
initiative:
  slug: t_fup_0848
  project_id: t_fup_0848
workspace_root: "$TMPROOT"
completion_predicate:
  - name: registry_drained
    check_kind: registry_zero_open
    params:
      path: "$reg"
      filter: "$filt"
---
body
EOF
}

# Run real stop_check.sh; echo its exit code.
run_sc() {
  local seed="$1" sd="$2"
  mkdir -p "$sd"
  bash "$RALPH_ROOT/hooks/stop_check.sh" "$seed" "$sd" >/dev/null 2>"$sd/err.txt"
  echo $?
}

# ---- Case 1: multi-table register (Preconditions FIRST, work table with Status, Change-History
# LAST), work items OPEN → expect rc=1 (continue), NOT rc=3, and NOT inflated by the trailing table.
REG1="$TMPROOT/Multi_Open_v1.0.md"
cat > "$REG1" <<'EOF'
# Register

| Precondition | State | Evidence |
|---|---|---|
| decisions ratified | Done | ref |
| migration applied | Done | probe |

## Work

| ID | Status | Title | Depends on |
|---|---|---|---|
| OLB-01 | open | skeleton | — |
| OLB-02 | open | registry adapter | OLB-01 |
| OLB-03 | closed | done item | — |

## Change History

| Version | Date | Author | Summary |
|---|---|---|---|
| 1.0 | 2026-06-04 | Claude | initial |
EOF
make_seed "$TMPROOT/seed1.md" "$REG1" "Status != closed"
RC=$(run_sc "$TMPROOT/seed1.md" "$TMPROOT/s1")
if [[ "$RC" == "1" ]]; then pass "[multi-table open] rc=1 continue (2 open OLB rows; not HALT, not Change-History-inflated)"
else fail "[multi-table open] expected rc=1, got rc=$RC :: $(cat "$TMPROOT/s1/err.txt")"; fi

# ---- Case 2: same shape, all work items CLOSED → expect rc=0 (complete). The trailing
# Change-History rows (Date != 'closed') must NOT inflate the count, or this regresses to rc=1.
REG2="$TMPROOT/Multi_Closed_v1.0.md"
cat > "$REG2" <<'EOF'
# Register

| Precondition | State | Evidence |
|---|---|---|
| decisions ratified | Done | ref |

## Work

| ID | Status | Title | Depends on |
|---|---|---|---|
| OLB-01 | closed | skeleton | — |
| OLB-02 | closed | registry adapter | OLB-01 |

## Change History

| Version | Date | Author | Summary |
|---|---|---|---|
| 1.0 | 2026-06-04 | Claude | initial |
EOF
make_seed "$TMPROOT/seed2.md" "$REG2" "Status != closed"
RC=$(run_sc "$TMPROOT/seed2.md" "$TMPROOT/s2")
if [[ "$RC" == "0" ]]; then pass "[multi-table all-closed] rc=0 complete (Change-History not counted)"
else fail "[multi-table all-closed] expected rc=0, got rc=$RC :: $(cat "$TMPROOT/s2/err.txt")"; fi

# ---- Case 3 (FUP-0829 preserved): register with NO Status column but a Priority column carrying
# an open **P2** → generic 'Status != closed' filter falls back to Priority open-count → rc=1.
REG3="$TMPROOT/Prio_Open_v1.0.md"
cat > "$REG3" <<'EOF'
# Gap Register

| ID | Name | Gap description | Priority | Prerequisites | Resolution path |
|---|---|---|---|---|---|
| G-01 | gap a | desc | **P2** | none | path.md |
| G-02 | gap b | desc | closed | none | path2.md |
EOF
make_seed "$TMPROOT/seed3.md" "$REG3" "Status != closed"
RC=$(run_sc "$TMPROOT/seed3.md" "$TMPROOT/s3")
if [[ "$RC" == "1" ]]; then pass "[priority fallback open] rc=1 continue (one **P2** open via FUP-0829 fallback)"
else fail "[priority fallback open] expected rc=1, got rc=$RC :: $(cat "$TMPROOT/s3/err.txt")"; fi

# ---- Case 4: Priority fallback, no open **Px** rows → rc=0 (complete).
REG4="$TMPROOT/Prio_Closed_v1.0.md"
cat > "$REG4" <<'EOF'
# Gap Register

| ID | Name | Gap description | Priority | Prerequisites | Resolution path |
|---|---|---|---|---|---|
| G-01 | gap a | desc | closed | none | path.md |
EOF
make_seed "$TMPROOT/seed4.md" "$REG4" "Status != closed"
RC=$(run_sc "$TMPROOT/seed4.md" "$TMPROOT/s4")
if [[ "$RC" == "0" ]]; then pass "[priority fallback closed] rc=0 complete (no open **Px**)"
else fail "[priority fallback closed] expected rc=0, got rc=$RC :: $(cat "$TMPROOT/s4/err.txt")"; fi

# ---- Case 5: no Status AND no Priority in any table → genuine HALT rc=3.
REG5="$TMPROOT/NoCol_v1.0.md"
cat > "$REG5" <<'EOF'
# Register

| Precondition | State | Evidence |
|---|---|---|
| x | Done | y |

## Change History

| Version | Date | Author | Summary |
|---|---|---|---|
| 1.0 | 2026-06-04 | Claude | initial |
EOF
make_seed "$TMPROOT/seed5.md" "$REG5" "Status != closed"
RC=$(run_sc "$TMPROOT/seed5.md" "$TMPROOT/s5")
if [[ "$RC" == "3" ]]; then pass "[no Status/Priority] rc=3 HALT (column genuinely absent)"
else fail "[no Status/Priority] expected rc=3, got rc=$RC :: $(cat "$TMPROOT/s5/err.txt")"; fi

# ---- Case 6: the column lives in the SECOND/THIRD table (Status), open items present → rc=1.
# Directly mirrors the ol-build register shape that exposed the bug.
make_seed "$TMPROOT/seed6.md" "$REG1" "Status != closed"
RC=$(run_sc "$TMPROOT/seed6.md" "$TMPROOT/s6")
if [[ "$RC" == "1" ]]; then pass "[ol-build shape] rc=1 (Status table is not first; found anyway)"
else fail "[ol-build shape] expected rc=1, got rc=$RC :: $(cat "$TMPROOT/s6/err.txt")"; fi

echo ""
echo "=== test_fup_0848_stop_check_multitable_register.sh: $PASS PASS, $FAIL FAIL ==="
[[ $FAIL -eq 0 ]]
