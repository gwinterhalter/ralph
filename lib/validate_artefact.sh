#!/usr/bin/env bash
# lib/validate_artefact.sh <schema_path> <json_path>
# JSON Schema (Draft 2020-12) validator helper for cross-skill data-contract enforcement
# (Initiative_Orchestrator_Spec_v1_4 §10.3 / §5.2). Phase 4a P4-06.
#
# Exit 0 — instance valid against schema
# Exit 1 — instance invalid (validator stderr printed)
# Exit 2 — neither ajv-cli nor python+jsonschema available on PATH
#
# Tooling preference: ajv-cli (fast, JSON-only). Fallback: python+jsonschema (already
# present on most dev boxes). Install either via:
#   npm i -g ajv-cli
#   python -m pip install jsonschema
set -euo pipefail
SCHEMA="${1:?usage: validate_artefact.sh <schema_path> <json_path>}"
INSTANCE="${2:?usage: validate_artefact.sh <schema_path> <json_path>}"
if [[ ! -f "$SCHEMA" ]]; then
  echo "validate_artefact: schema not found: $SCHEMA" >&2
  exit 1
fi
if [[ ! -f "$INSTANCE" ]]; then
  echo "validate_artefact: instance not found: $INSTANCE" >&2
  exit 1
fi
if command -v ajv >/dev/null 2>&1; then
  ajv validate -s "$SCHEMA" -d "$INSTANCE" --spec=draft2020 --strict=false
elif command -v python >/dev/null 2>&1; then
  python -c "import sys,json,jsonschema; jsonschema.validate(instance=json.load(open(sys.argv[2])), schema=json.load(open(sys.argv[1])))" "$SCHEMA" "$INSTANCE"
else
  echo "validate_artefact: neither ajv nor python+jsonschema available on PATH" >&2
  exit 2
fi
