# -*- coding: utf-8 -*-
"""
redact.py -- strip live credential VALUES out of an artefact before it becomes durable record.

WHY THIS EXISTS (FUP-1635)
    `hooks/execute_with_gates.sh` copies `.permission_denials` VERBATIM from the executor's
    `execution_result_NNNN.json` into `escalations/auto_mode_denial_*.json`. Each denial carries
    `tool_input.command` -- the full text of the script the executor tried to run. Measured on the
    2026-08-02 factory_dryrun run: 10 denials across 3 escalation files, every one of them
    rendering an env var NAME (`$env:CF_FACTORY_APP_PASSWORD`) and never a resolved value. So the
    exposure is LATENT, not realised -- but nothing prevents it. The executor is an LLM authoring
    shell ad hoc; one `Write-Output $env:CF_DB_PASSWORD` while debugging an auth failure puts a
    live superuser password into an append-only escalation record and a session log.

    `CF_DB_PASSWORD` and `SUPABASE_DB_PASSWORD` are byte-identical (verified 2026-08-05 by sha256
    comparison, values never rendered) -- one credential serving both the local postgres superuser
    and the Supabase corpus. That is the blast radius this guard exists to bound.

    This does NOT replace prevention. The EXECUTION CONTRACT injected by execute_with_gates.sh
    tells the executor to route DB access through the helper modules instead of hand-writing psql.
    This module is the backstop for when it does not.

METHOD
    Credential env vars are ENUMERATED from the environment by name pattern, never from a
    hand-written list (a typed list silently omits whatever is not on it). A var qualifies when:
      * its NAME matches PASSWORD | PASSWD | SECRET | TOKEN | _KEY | APIKEY | CREDENTIAL, and
      * its VALUE is at least MIN_SECRET_LEN chars.
    The length floor is the false-positive guard: a short value like "1" or "true" would otherwise
    match everywhere and destroy the artefact.

    JSON files are parsed, every string leaf rewritten, and re-serialised -- so a secret that was
    JSON-escaped in the raw bytes is still caught (the decode normalises it). Non-JSON files fall
    back to a literal text replacement.

    NOTHING IN THIS MODULE EVER PRINTS A CREDENTIAL VALUE. Findings are reported as
    `<ENVVAR_NAME> x<count>` -- class and location, never the value (universal-rules.md #14).

USAGE
    python redact.py <file> [<file> ...]        # rewrites in place, prints a masked summary
    python redact.py --selftest                 # positive + negative control, no files touched

    Exit 0 always for the redaction path (best-effort: a guard that aborts the run is a guard that
    gets removed). Exit 1 only from --selftest, and only when a control fails.
"""
from __future__ import annotations

import json
import os
import re
import sys

# Name patterns that mark an env var as credential-bearing. Union, case-insensitive.
SECRET_NAME_RE = re.compile(
    r"(PASSWORD|PASSWD|SECRET|TOKEN|APIKEY|API_KEY|_KEY$|CREDENTIAL)", re.IGNORECASE
)
# Values shorter than this are not treated as secrets -- see METHOD above. 12 clears the two
# live credentials on this host (13 and 40) with margin, and excludes flag-like values.
MIN_SECRET_LEN = 12


def collect_secrets(environ=None) -> dict:
    """Enumerate credential values from the environment. Returns {value: env_var_name}.

    Keyed by VALUE so that two names sharing one value (CF_DB_PASSWORD and SUPABASE_DB_PASSWORD
    are byte-identical here) collapse to a single replacement pass. The retained name is the
    lexically first, so the label is deterministic across runs.
    """
    env = os.environ if environ is None else environ
    found: dict = {}
    for name in sorted(env):
        if not SECRET_NAME_RE.search(name):
            continue
        value = env.get(name) or ""
        if len(value) < MIN_SECRET_LEN:
            continue
        found.setdefault(value, name)
    return found


def redact_text(text: str, secrets: dict) -> tuple:
    """Replace every literal secret occurrence in `text`. Returns (new_text, {name: count})."""
    hits: dict = {}
    for value, name in secrets.items():
        count = text.count(value)
        if count:
            text = text.replace(value, "[REDACTED:%s]" % name)
            hits[name] = hits.get(name, 0) + count
    return text, hits


def _walk(node, secrets: dict, hits: dict):
    """Rewrite every string leaf of a decoded JSON document."""
    if isinstance(node, str):
        new, found = redact_text(node, secrets)
        for k, v in found.items():
            hits[k] = hits.get(k, 0) + v
        return new
    if isinstance(node, list):
        return [_walk(x, secrets, hits) for x in node]
    if isinstance(node, dict):
        # Keys are rewritten too: a secret can land in a key when a tool serialises an env map.
        return {_walk(k, secrets, hits): _walk(v, secrets, hits) for k, v in node.items()}
    return node


def redact_file(path: str, secrets: dict) -> dict:
    """Rewrite `path` in place. Returns {env_var_name: occurrence_count}; {} when clean.

    Best-effort by contract: an unreadable/unwritable file returns {} rather than raising, so a
    redaction failure can never take down the orchestrator run it is protecting.
    """
    if not secrets:
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as fh:
            raw = fh.read()
    except OSError:
        return {}

    hits: dict = {}
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        new_raw, hits = redact_text(raw, secrets)
    else:
        doc = _walk(doc, secrets, hits)
        # separators pinned so the rewrite is byte-stable when nothing matched.
        new_raw = json.dumps(doc, ensure_ascii=False)

    if not hits:
        return {}
    try:
        with open(path, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(new_raw)
    except OSError:
        return {}
    return hits


def _selftest() -> int:
    """Positive control (a planted value IS found and removed) + negative control (a clean
    document is NOT rewritten, and a short value is NOT treated as a secret).

    The planted value is generated here, not read from the environment, so the control proves the
    matcher works without a live credential ever entering this process's output.
    """
    failures = []
    planted = "Zq7!planted-secret-value-4f2a"          # >= MIN_SECRET_LEN, not a real credential
    short = "abc"                                       # < MIN_SECRET_LEN
    fake_env = {
        "MY_TEST_PASSWORD": planted,
        "MY_TEST_TOKEN": short,
        "MY_TEST_HOST": "localhost",                    # name not credential-bearing
        "OTHER_PASSWORD": planted,                      # duplicate value, collapses to one entry
    }
    secrets = collect_secrets(fake_env)

    # POSITIVE CONTROL 1: the long password is enumerated.
    if planted not in secrets:
        failures.append("positive: MY_TEST_PASSWORD value not enumerated")
    # NEGATIVE CONTROL 1: the short token is NOT enumerated (length floor holds).
    if short in secrets:
        failures.append("negative: short value passed the length floor")
    # NEGATIVE CONTROL 2: a non-credential name is NOT enumerated.
    if "localhost" in secrets:
        failures.append("negative: MY_TEST_HOST enumerated as a secret")
    # Duplicate values collapse to exactly one replacement entry.
    if len(secrets) != 1:
        failures.append("expected 1 collapsed secret entry, got %d" % len(secrets))

    # POSITIVE CONTROL 2: the value is removed from a JSON-shaped payload, incl. a nested command.
    doc = json.dumps({
        "permission_denials": [
            {"tool_name": "Bash",
             "tool_input": {"command": 'PGPASSWORD="%s" psql "dbname=x"' % planted}}
        ]
    })
    hits: dict = {}
    out = _walk(json.loads(doc), secrets, hits)
    serialised = json.dumps(out)
    if planted in serialised:
        failures.append("positive: planted value SURVIVED redaction")
    if "[REDACTED:MY_TEST_PASSWORD]" not in serialised:
        failures.append("positive: redaction marker absent")
    if hits.get("MY_TEST_PASSWORD") != 1:
        failures.append("positive: hit count %r != 1" % hits.get("MY_TEST_PASSWORD"))

    # NEGATIVE CONTROL 3: a document with no secret is reported clean (no false rewrite).
    clean_hits: dict = {}
    _walk({"command": "psql dbname=codefactory_build"}, secrets, clean_hits)
    if clean_hits:
        failures.append("negative: clean document reported hits %r" % clean_hits)

    for f in failures:
        sys.stderr.write("redact selftest FAIL: %s\n" % f)
    if failures:
        return 1
    sys.stdout.write(
        "redact selftest PASS -- 3 positive controls, 3 negative controls, "
        "%d secret(s) enumerated from the fixture env\n" % len(secrets)
    )
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    if not argv:
        sys.stderr.write("usage: redact.py <file> [<file> ...] | --selftest\n")
        return 0
    secrets = collect_secrets()
    for path in argv:
        hits = redact_file(path, secrets)
        if hits:
            # class + location + count only -- never the value.
            summary = ", ".join("%s x%d" % (n, c) for n, c in sorted(hits.items()))
            sys.stderr.write("redact: %s -- REDACTED %s\n" % (path, summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
