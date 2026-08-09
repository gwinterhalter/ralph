#!/usr/bin/env bash
# tests/test_db_zero_open_predicate.sh
# db_zero_open: the completion predicate must count OPEN ITEMS IN THE DATABASE,
# and must FAIL CLOSED whenever it cannot see them.
#
# WHY IT EXISTS: `registry_zero_open`/`zero_open_gaps` counts pipe-delimited Priority cells in
# a STATIC markdown register. An item that never reaches that file cannot affect completion at
# ANY value, and a Phase-Z INSERT into the tracker can GROW the backlog while the loop still
# reports INITIATIVE_COMPLETE. This test drives the REAL hooks/stop_check.sh and proves the new
# arm refuses on non-zero, passes on a genuine zero, and blocks (never passes) when the database
# is unreachable, the credential is missing, the scope is absent/unvalidatable, or the result
# does not parse.
#
# A PREDICATE ONLY EVER SEEN GREEN IS THE DEFECT CLASS THIS WORKSTREAM KEEPS FINDING, so every
# zero below carries a POSITIVE CONTROL: the identical code path, differing only in the scope,
# must return non-zero. Without it, "0 rows" and "broken query" are the same observation.
#
# Exit contract under test (stop_check.sh header): 0 = all predicates pass (complete);
# 1 = at least one failed (continue); >=3 = malformed/error (HALT).
#
# Run: bash tests/test_db_zero_open_predicate.sh     (exit 0 = all PASS)
# Needs: psql on PATH or at PSQL_EXE, and SUPABASE_DB_PASSWORD (Machine scope) for the live
# cases. Without the credential the live cases SKIP loudly and the config-HALT cases still run;
# a skip is reported, never silently counted as a pass.

set -uo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RALPH_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

PASS=0; FAIL=0; SKIP=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*" >&2; FAIL=$((FAIL+1)); }
skip() { echo "SKIP: $*"; SKIP=$((SKIP+1)); }

# Seed carrying a single db_zero_open predicate. No .work_registry, so the unconditional
# closure-contract guard reports N/A and this test isolates the arm under test.
make_seed() {
  local seed="$1" scope="$2" table="${3:-}"
  {
    echo "---"
    echo "initiative:"
    echo "  slug: t_db_zero_open"
    echo "  project_id: t_db_zero_open"
    echo "workspace_root: \"$TMPROOT\""
    echo "completion_predicate:"
    echo "  - name: zero_open_followups_db"
    echo "    check_kind: db_zero_open"
    echo "    params:"
    [[ -n "$scope" ]] && echo "      scope_sql: \"$scope\""
    [[ -n "$table" ]] && echo "      table: \"$table\""
    echo "---"
    echo "body"
  } > "$seed"
}

# Run the real hook; echo its exit code. stderr is kept for marker assertions.
run_sc() {
  local seed="$1" sd="$2"
  mkdir -p "$sd"
  bash "$RALPH_ROOT/hooks/stop_check.sh" "$seed" "$sd" >/dev/null 2>"$sd/err.txt"
  echo $?
}

HAVE_DB=1
[[ -n "${SUPABASE_DB_PASSWORD:-}" ]] || HAVE_DB=0

# ---------------------------------------------------------------------------
# (a) NON-ZERO -> refuses. The real open set for the initiative this loop drains.
# ---------------------------------------------------------------------------
if [[ "$HAVE_DB" -eq 1 ]]; then
  make_seed "$TMPROOT/seed_a.md" "project_id = 'factory_design' AND status = 'pending'"
  RC=$(run_sc "$TMPROOT/seed_a.md" "$TMPROOT/a")
  ERR="$(cat "$TMPROOT/a/err.txt")"
  OPEN_A="$(sed -nE 's/.*OPEN=([0-9]+).*/\1/p' <<< "$ERR" | head -1)"
  if [[ "$RC" == "1" && -n "$OPEN_A" && "$OPEN_A" -gt 0 && "$ERR" == *"blocking completion"* ]]; then
    pass "[a non-zero] rc=1 continue; measured OPEN=$OPEN_A open followups blocked completion"
  else
    fail "[a non-zero] expected rc=1 with OPEN>0, got rc=$RC :: $ERR"
  fi
else
  skip "[a non-zero] SUPABASE_DB_PASSWORD unset"
fi

# ---------------------------------------------------------------------------
# (b) ZERO -> passes, WITH ITS POSITIVE CONTROL.
#     b1: a project_id that does not exist -> 0 -> rc=0.
#     b2 (POSITIVE CONTROL): the identical query shape, differing ONLY in the scope value,
#         must return non-zero. If b2 also returned 0 the mechanism would be broken, not the
#         data, and b1's pass would be worthless.
# ---------------------------------------------------------------------------
if [[ "$HAVE_DB" -eq 1 ]]; then
  make_seed "$TMPROOT/seed_b1.md" "project_id = 'zzz_nonexistent_qqq' AND status = 'pending'"
  RC=$(run_sc "$TMPROOT/seed_b1.md" "$TMPROOT/b1")
  ERR="$(cat "$TMPROOT/b1/err.txt")"
  if [[ "$RC" == "0" && "$ERR" == *"OPEN=0"* && "$ERR" == *"pass"* ]]; then
    pass "[b1 zero] rc=0 complete; OPEN=0 for a project_id that does not exist"
  else
    fail "[b1 zero] expected rc=0 with OPEN=0, got rc=$RC :: $ERR"
  fi

  make_seed "$TMPROOT/seed_b2.md" "project_id = 'factory_design' AND status = 'pending'"
  RC=$(run_sc "$TMPROOT/seed_b2.md" "$TMPROOT/b2")
  ERR="$(cat "$TMPROOT/b2/err.txt")"
  CTRL="$(sed -nE 's/.*OPEN=([0-9]+).*/\1/p' <<< "$ERR" | head -1)"
  if [[ "$RC" == "1" && -n "$CTRL" && "$CTRL" -gt 0 ]]; then
    pass "[b2 POSITIVE CONTROL] same code path returned OPEN=$CTRL (>0) — b1's zero is data, not a broken query"
  else
    fail "[b2 POSITIVE CONTROL] control did not fire; b1's zero is UNPROVEN :: rc=$RC :: $ERR"
  fi

  # Boundary sensitivity, end to end and with no write: a scope matching exactly ONE row must
  # block, and the adjacent scope matching none must pass. This is the 0-vs-1 transition the
  # predicate exists to detect, proven through the hook rather than through the query alone.
  make_seed "$TMPROOT/seed_b3.md" "followup_id = (SELECT min(followup_id) FROM public.followups)"
  RC=$(run_sc "$TMPROOT/seed_b3.md" "$TMPROOT/b3")
  ERR3="$(cat "$TMPROOT/b3/err.txt")"
  # NB: followups.followup_id is TEXT here, not an integer — `followup_id = -1` raises
  # "operator does not exist: text = integer" and the arm correctly reports DB-UNREACHABLE
  # rather than reading the error as zero. Measured 2026-08-08; use a text literal.
  make_seed "$TMPROOT/seed_b4.md" "followup_id = 'zzz_no_such_followup_qqq'"
  RC4=$(run_sc "$TMPROOT/seed_b4.md" "$TMPROOT/b4")
  ERR4="$(cat "$TMPROOT/b4/err.txt")"
  if [[ "$RC" == "1" && "$ERR3" == *"OPEN=1"* && "$RC4" == "0" && "$ERR4" == *"OPEN=1"* ]]; then
    fail "[b3/b4 boundary] both scopes reported OPEN=1 — the scope is not being applied"
  elif [[ "$RC" == "1" && "$ERR3" == *"OPEN=1"* && "$RC4" == "0" && "$ERR4" == *"OPEN=0"* ]]; then
    pass "[b3/b4 boundary] exactly-one-row scope -> rc=1 OPEN=1; no-row scope -> rc=0 OPEN=0 (0-vs-1 transition observed end to end)"
  else
    fail "[b3/b4 boundary] expected rc=1/OPEN=1 then rc=0/OPEN=0, got rc=$RC :: $ERR3 // rc=$RC4 :: $ERR4"
  fi
else
  skip "[b zero + positive control + boundary] SUPABASE_DB_PASSWORD unset"
fi

# ---------------------------------------------------------------------------
# (c) FAIL CLOSED. Four independent ways of not being able to see the target; every one must
#     BLOCK, and every one must be distinguishable in the log from a genuine non-zero.
# ---------------------------------------------------------------------------
# c1: unreachable host.
make_seed "$TMPROOT/seed_c1.md" "project_id = 'factory_design' AND status = 'pending'"
mkdir -p "$TMPROOT/c1"
( export SUPABASE_DB_HOST=127.0.0.1 SUPABASE_DB_PORT=1 SUPABASE_DB_CONNECT_TIMEOUT=5 PGCONNECT_TIMEOUT=5
  bash "$RALPH_ROOT/hooks/stop_check.sh" "$TMPROOT/seed_c1.md" "$TMPROOT/c1" >/dev/null 2>"$TMPROOT/c1/err.txt" )
RC=$?
ERR="$(cat "$TMPROOT/c1/err.txt")"
if [[ "$RC" == "1" && "$ERR" == *"DB-UNREACHABLE"* && "$ERR" != *"OPEN="* ]]; then
  pass "[c1 unreachable host] rc=1 blocked, logged DB-UNREACHABLE, emitted no OPEN= count"
else
  fail "[c1 unreachable host] expected rc=1 + DB-UNREACHABLE + no OPEN=, got rc=$RC :: $ERR"
fi

# c2: credential missing.
mkdir -p "$TMPROOT/c2"
( unset SUPABASE_DB_PASSWORD
  bash "$RALPH_ROOT/hooks/stop_check.sh" "$TMPROOT/seed_c1.md" "$TMPROOT/c2" >/dev/null 2>"$TMPROOT/c2/err.txt" )
RC=$?
ERR="$(cat "$TMPROOT/c2/err.txt")"
if [[ "$RC" == "1" && "$ERR" == *"DB-UNREACHABLE"* && "$ERR" == *"SUPABASE_DB_PASSWORD is unset"* ]]; then
  pass "[c2 no credential] rc=1 blocked with DB-UNREACHABLE (never a silent skip, never a pass)"
else
  fail "[c2 no credential] expected rc=1 + DB-UNREACHABLE, got rc=$RC :: $ERR"
fi

# c3: the query itself errors (a column that does not exist). psql exits non-zero; the arm must
#     treat that as unreachable, not as zero.
if [[ "$HAVE_DB" -eq 1 ]]; then
  make_seed "$TMPROOT/seed_c3.md" "zzz_no_such_column_qqq = 1"
  RC=$(run_sc "$TMPROOT/seed_c3.md" "$TMPROOT/c3")
  ERR="$(cat "$TMPROOT/c3/err.txt")"
  if [[ "$RC" == "1" && "$ERR" == *"DB-UNREACHABLE"* ]]; then
    pass "[c3 query error] rc=1 blocked with DB-UNREACHABLE — a failed query is never read as zero"
  else
    fail "[c3 query error] expected rc=1 + DB-UNREACHABLE, got rc=$RC :: $ERR"
  fi
else
  skip "[c3 query error] SUPABASE_DB_PASSWORD unset"
fi

# c4: no psql binary reachable. STATED AS UNREACHABLE ON THIS HOST rather than asserted, per
# the "prove every control can FIRE" rule: the arm resolves psql as PSQL_EXE -> the hardcoded
# Windows path -> `command -v psql`, so while the Windows path exists this branch cannot be
# reached by any env manipulation, and emptying PATH breaks `yq` first — the hook would then
# die for an unrelated reason and the assertion would pass for the wrong one. c2 exercises the
# same fail-closed sink (a precondition the arm checks before dialling), so the branch is not
# unguarded; only this ONE of its two entry conditions is untested here.
WIN_PSQL="C:/Program Files/PostgreSQL/18/bin/psql.exe"
if [[ -x "$WIN_PSQL" ]]; then
  skip "[c4 no psql] branch unreachable on this host — '$WIN_PSQL' exists, so the fallback always resolves; asserting it here would pass for the wrong reason"
else
  make_seed "$TMPROOT/seed_c4.md" "project_id = 'factory_design' AND status = 'pending'"
  mkdir -p "$TMPROOT/c4"
  ( export PSQL_EXE="$TMPROOT/definitely_not_psql"
    bash "$RALPH_ROOT/hooks/stop_check.sh" "$TMPROOT/seed_c4.md" "$TMPROOT/c4" >/dev/null 2>"$TMPROOT/c4/err.txt" )
  RC=$?
  ERR="$(cat "$TMPROOT/c4/err.txt")"
  if [[ "$RC" == "1" && "$ERR" == *"no psql binary found"* ]]; then
    pass "[c4 no psql] rc=1 blocked with DB-UNREACHABLE — no psql binary found"
  else
    fail "[c4 no psql] expected rc=1 + 'no psql binary found', got rc=$RC :: $ERR"
  fi
fi

# ---------------------------------------------------------------------------
# CONFIG FAIL-CLOSED: an unvalidatable scope HALTs. It must never default to a permissive
# predicate. These run without a database.
# ---------------------------------------------------------------------------
make_seed "$TMPROOT/seed_d1.md" ""            # no params.scope_sql at all
RC=$(run_sc "$TMPROOT/seed_d1.md" "$TMPROOT/d1")
ERR="$(cat "$TMPROOT/d1/err.txt")"
if [[ "$RC" == "3" && "$ERR" == *"missing params.scope_sql"* ]]; then
  pass "[cfg1 absent scope] rc=3 HALT — an absent scope fails closed rather than matching everything"
else
  fail "[cfg1 absent scope] expected rc=3, got rc=$RC :: $ERR"
fi

make_seed "$TMPROOT/seed_d2.md" "status = 'pending'; DROP TABLE x"
RC=$(run_sc "$TMPROOT/seed_d2.md" "$TMPROOT/d2")
ERR="$(cat "$TMPROOT/d2/err.txt")"
if [[ "$RC" == "3" && "$ERR" == *"may not contain"* ]]; then
  pass "[cfg2 multi-statement scope] rc=3 HALT — ';' / '--' / '/*' rejected before any DB call"
else
  fail "[cfg2 multi-statement scope] expected rc=3, got rc=$RC :: $ERR"
fi

make_seed "$TMPROOT/seed_d3.md" "status = 'pending'" "public.followups WHERE 1=1 --"
RC=$(run_sc "$TMPROOT/seed_d3.md" "$TMPROOT/d3")
ERR="$(cat "$TMPROOT/d3/err.txt")"
if [[ "$RC" == "3" && "$ERR" == *"not a plain"* ]]; then
  pass "[cfg3 bad table] rc=3 HALT — params.table must be a plain [schema.]table identifier"
else
  fail "[cfg3 bad table] expected rc=3, got rc=$RC :: $ERR"
fi

# NEGATIVE CONTROL for the config HALTs: a well-formed scope must NOT HALT. Without this the
# three passes above are satisfiable by an arm that HALTs unconditionally.
if [[ "$HAVE_DB" -eq 1 ]]; then
  make_seed "$TMPROOT/seed_d4.md" "project_id = 'zzz_nonexistent_qqq'" "public.followups"
  RC=$(run_sc "$TMPROOT/seed_d4.md" "$TMPROOT/d4")
  if [[ "$RC" == "0" ]]; then
    pass "[cfg4 NEGATIVE CONTROL] a well-formed scope + explicit table does NOT HALT (rc=0)"
  else
    fail "[cfg4 NEGATIVE CONTROL] well-formed scope unexpectedly rc=$RC :: $(cat "$TMPROOT/d4/err.txt")"
  fi
else
  skip "[cfg4 negative control] SUPABASE_DB_PASSWORD unset"
fi

# ---------------------------------------------------------------------------
echo "----"
echo "PASS=$PASS FAIL=$FAIL SKIP=$SKIP"
[[ "$FAIL" -eq 0 ]] || exit 1
[[ "$PASS" -gt 0 ]] || { echo "no assertion ran — treating as failure" >&2; exit 1; }
exit 0
