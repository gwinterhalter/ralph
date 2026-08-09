#!/usr/bin/env bash
# lib/path_guard.sh — write-containment assertions (FUP-1483).
#
# WHAT ACTUALLY HAPPENED, corrected. FUP-1483 was filed as "a path concatenation produced a
# doubled base and the run mirrored a tree onto the OneDrive drive". The concatenation
# hypothesis is WRONG: the iteration-0012 Executor transcript shows the doubled path was a
# hand-typed absolute literal in a single Write tool call
#     K:\OneDrive - EPM Solutions - EPM Solutions - Project Server- Project Online\...
# (the agent said so itself in execution_result_0012.json), and the Write tool created the
# whole intermediate directory chain from that one call. No shell or Python join in this
# repo can produce that string, and grep confirms none does.
#
# So the fixable defect is NOT the concatenation. It is that the harness has NO write
# containment at all: it declares read_only_paths (a deny-list of places nobody may write)
# and enforces it at execute_with_gates.sh:447-461, but it never asserts the positive —
# that a path being written is INSIDE the sandbox. A deny-list cannot catch a typo that
# lands somewhere nobody thought to deny. That is the gap this file closes.
#
# Two complementary guards, because one alone would not have caught this incident:
#
#   pg_assert_under  — for paths the HARNESS composes. Deterministic, fires at compose
#                      time. This is what catches the real (and still live) double-join
#                      class, e.g. lib/command_dispatch.sh joining an already-absolute
#                      .work_registry onto .workspace_root.
#   pg_snapshot_fsroot / pg_check_fsroot — for paths an AGENT composes, which the harness
#                      cannot intercept. Baselines the top-level entries of the workspace
#                      drive at run start and RAISEs when the run creates a new one. The
#                      doubled tree was a new top-level directory on K:, so this is a
#                      direct detector for the damage shape actually observed.

# --- pg_assert_under <label> <path> <root>... ------------------------------------------
# Exit 0 when <path> resolves under at least one <root>. Exit 1 + stderr otherwise.
# Comparison is on normalised strings: backslashes to forward, repeated slashes collapsed,
# case folded (Windows), trailing slash dropped. A sibling-prefix guard is essential —
# "/a/bc" must NOT count as being under "/a/b" — hence the explicit "/" on the prefix test.
pg_assert_under() {
  local _label="$1" _path="$2"; shift 2
  local _norm _r _rn
  _norm="$(printf '%s' "$_path" | tr '\\' '/' | tr -s '/' | tr 'A-Z' 'a-z')"
  _norm="${_norm%/}"
  for _r in "$@"; do
    [[ -n "$_r" ]] || continue
    _rn="$(printf '%s' "$_r" | tr '\\' '/' | tr -s '/' | tr 'A-Z' 'a-z')"
    _rn="${_rn%/}"
    [[ "$_norm" == "$_rn" || "$_norm" == "$_rn"/* ]] && return 0
  done
  echo "path_guard: WRITE OUTSIDE SANDBOX refused — $_label resolves to '$_path'" >&2
  echo "path_guard: declared roots: $*" >&2
  return 1
}

# --- pg_snapshot_fsroot <state_dir> <workspace_root> [watch_root] ----------------------
# Record the top-level entries of the filesystem root that holds the workspace, once per
# run. Cheap (one listing) and taken before any LLM call can have created anything.
# <watch_root> overrides the derived drive root. The orchestrator omits it; callers that
# know the container to watch (and the control suite) pass it explicitly, because the
# derivation below only produces a meaningful answer for a "X:/..." drive-letter path.
pg_snapshot_fsroot() {
  local _sd="$1" _ws="$2" _watch="${3:-}"
  [[ -n "$_sd" && -d "$_sd" ]] || return 0
  local _fsroot
  if [[ -n "$_watch" ]]; then
    _fsroot="${_watch%/}/"
  else
    _fsroot="$(printf '%s' "$_ws" | tr '\\' '/' | cut -d/ -f1)/"
  fi
  [[ -d "$_fsroot" ]] || return 0
  printf '%s\n' "$_fsroot" > "$_sd/.fsroot_path" 2>/dev/null || true
  ls -1A "$_fsroot" 2>/dev/null | sort > "$_sd/.fsroot_baseline" 2>/dev/null || true
  return 0
}

# --- pg_check_fsroot <state_dir> ------------------------------------------------------
# Exit 0 = no new top-level entry. Exit 1 = the run created one -> RAISE.
# Deliberately reports rather than deletes: an out-of-sandbox artifact is evidence, and
# this guard must never itself become a destructive actor on an unvetted path.
pg_check_fsroot() {
  local _sd="$1"
  [[ -f "$_sd/.fsroot_baseline" && -f "$_sd/.fsroot_path" ]] || return 0
  local _fsroot _new
  _fsroot="$(cat "$_sd/.fsroot_path" 2>/dev/null)"
  [[ -n "$_fsroot" && -d "$_fsroot" ]] || return 0
  _new="$(comm -13 "$_sd/.fsroot_baseline" <(ls -1A "$_fsroot" 2>/dev/null | sort) 2>/dev/null || true)"
  if [[ -n "$_new" ]]; then
    echo "path_guard: SANDBOX ESCAPE — the run created new top-level entries under $_fsroot:" >&2
    # Read line-by-line rather than word-splitting: the names this guard exists to report
    # CONTAIN SPACES ("OneDrive - EPM Solutions - ..."), and an unquoted expansion would
    # shatter the one string an operator needs to see into unrecognisable fragments.
    while IFS= read -r _entry; do [[ -n "$_entry" ]] && printf '  %s\n' "$_entry" >&2; done <<< "$_new"
    echo "path_guard: this is the FUP-1483 signature (a mistyped absolute path whose parent dirs were auto-created)." >&2
    echo "path_guard: inspect and remove them before continuing; they are NOT deleted automatically." >&2
    return 1
  fi
  return 0
}

# --- CLI mode --------------------------------------------------------------------------
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  set -uo pipefail
  case "${1:-}" in
    assert)   _l="$2"; _p="$3"; shift 3; pg_assert_under "$_l" "$_p" "$@" ;;
    snapshot) pg_snapshot_fsroot "$2" "$3" "${4:-}" ;;
    check)    pg_check_fsroot "$2" ;;
    *) echo "usage: path_guard.sh {assert <label> <path> <root>...|snapshot <state_dir> <workspace_root>|check <state_dir>}" >&2; exit 64 ;;
  esac
fi
