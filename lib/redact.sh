#!/usr/bin/env bash
# lib/redact.sh — strip live credential VALUES out of an artefact before it becomes durable record.
#
# FUP-1635. Thin shell wrapper around lib/redact.py; see that file's docstring for the WHY and the
# enumeration method. Sourced by hooks/execute_with_gates.sh and applied to every escalation
# artefact (and to the executor's own execution_result) at write time.
#
# CONTRACT — best-effort, never fatal. A guard that can abort the run it protects is a guard the
# next operator deletes. Every failure path here returns 0; the only observable effect of a broken
# redactor is that nothing is redacted, which is the status quo ante.
#
# Never echoes a credential value. redact.py reports `<ENVVAR_NAME> x<count>` to stderr and nothing
# more (universal-rules.md #14).

# Resolved from THIS file's own location, captured at source time. Deliberately not derived from
# the caller's $SCRIPT_DIR: execute_with_gates.sh sources this from hooks/, so a caller-relative
# path would resolve to hooks/redact.py and silently disable the guard.
_REDACT_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/redact.py"

# FUP-0832 discipline, copied from lib/validate_artefact.sh: a bare `command -v python` matches the
# Microsoft Store App-Execution-Alias shim, which prints "Python was not found" and exits non-zero
# instead of running. Resolve an interpreter that ACTUALLY executes.
_redact_resolve_python() {
  local b
  for b in python python3 py; do
    command -v "$b" >/dev/null 2>&1 || continue
    if "$b" -c "import sys" >/dev/null 2>&1; then echo "$b"; return 0; fi
  done
  return 1
}

# redact_artefact <file> [<file> ...]
# Rewrites each existing file in place, replacing any literal occurrence of a credential value
# drawn from this process's environment with `[REDACTED:<ENVVAR_NAME>]`.
redact_artefact() {
  local pybin script script_native f natives=()
  [[ $# -eq 0 ]] && return 0

  script="$_REDACT_PY"
  if [[ ! -f "$script" ]]; then
    echo "redact: lib/redact.py not found at $script — artefact(s) written UNREDACTED" >&2
    return 0
  fi

  pybin="$(_redact_resolve_python)" || {
    echo "redact: no working python on PATH — artefact(s) written UNREDACTED" >&2
    return 0
  }

  # FUP-0746: MSYS_NO_PATHCONV=1 is set by orchestrator.sh, so /k/... paths are NOT auto-converted
  # for native python.exe. Convert explicitly; pass through on non-MSYS hosts.
  if command -v cygpath >/dev/null 2>&1; then
    script_native="$(cygpath -w "$script")"
  else
    script_native="$script"
  fi
  for f in "$@"; do
    [[ -f "$f" ]] || continue
    if command -v cygpath >/dev/null 2>&1; then
      natives+=("$(cygpath -w "$f")")
    else
      natives+=("$f")
    fi
  done
  [[ ${#natives[@]} -eq 0 ]] && return 0

  "$pybin" "$script_native" "${natives[@]}" || true
  return 0
}

# redact_selftest — positive + negative controls on the matcher. Used by the test suite; safe to
# run at any time (touches no files, plants its own fixture value, reads no live credential).
redact_selftest() {
  local pybin script script_native
  script="$_REDACT_PY"
  pybin="$(_redact_resolve_python)" || return 1
  if command -v cygpath >/dev/null 2>&1; then
    script_native="$(cygpath -w "$script")"
  else
    script_native="$script"
  fi
  "$pybin" "$script_native" --selftest
}
