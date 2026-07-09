"""Dedicated unit tests for supervisor.seed_validation.SeedReviewValidator — the live
structural SS-* seed check that gates candidate -> admitted (a SEVERE finding blocks admission).

Covers: no seed_path, missing file, no frontmatter, each/all missing required keys, clean seed.
"""
from __future__ import annotations

from pathlib import Path

from supervisor.admission import SEVERITY_SEVERE
from supervisor.seed_validation import (
    REQUIRED_SEED_KEYS,
    SS_MISSING_REQUIRED_FIELD,
    SS_NO_FRONTMATTER,
    SS_SEED_MISSING,
    SeedReviewValidator,
)

V = SeedReviewValidator()


def _codes(findings) -> set[str]:
    return {f.code for f in findings}


def _write(tmp_path: Path, text: str) -> str:
    p = tmp_path / "seed.md"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_no_seed_path_is_severe_missing() -> None:
    findings = V.validate_seed({})
    assert _codes(findings) == {SS_SEED_MISSING}
    assert all(f.severity == SEVERITY_SEVERE for f in findings)


def test_seed_file_absent_is_severe_missing(tmp_path: Path) -> None:
    findings = V.validate_seed({"seed_path": str(tmp_path / "does_not_exist.md")})
    assert _codes(findings) == {SS_SEED_MISSING}


def test_no_frontmatter_flagged(tmp_path: Path) -> None:
    seed = _write(tmp_path, "# just a body\nno yaml fences here\n")
    findings = V.validate_seed({"seed_path": seed})
    assert _codes(findings) == {SS_NO_FRONTMATTER}
    assert all(f.severity == SEVERITY_SEVERE for f in findings)


def test_single_missing_required_key(tmp_path: Path) -> None:
    # frontmatter present but missing completion_predicate
    seed = _write(
        tmp_path,
        "---\nworkspace_root: /x\nstate_dir_relative: state/\nwork_registry: r.md\n---\nbody\n",
    )
    findings = V.validate_seed({"seed_path": seed})
    assert _codes(findings) == {SS_MISSING_REQUIRED_FIELD}
    assert len(list(findings)) == 1
    assert any("completion_predicate" in f.detail for f in findings)


def test_all_required_keys_missing_one_finding_each(tmp_path: Path) -> None:
    seed = _write(tmp_path, "---\ninitiative: {}\n---\nbody\n")
    findings = list(V.validate_seed({"seed_path": seed}))
    assert _codes(findings) == {SS_MISSING_REQUIRED_FIELD}
    assert len(findings) == len(REQUIRED_SEED_KEYS)  # one SS-003 per absent required key


def test_indented_key_does_not_count_as_top_level(tmp_path: Path) -> None:
    # a nested `workspace_root:` under some parent must NOT satisfy the top-level requirement
    seed = _write(
        tmp_path,
        "---\nparent:\n  workspace_root: /x\nstate_dir_relative: state/\n"
        "work_registry: r.md\ncompletion_predicate:\n  - name: x\n---\nbody\n",
    )
    findings = V.validate_seed({"seed_path": seed})
    assert SS_MISSING_REQUIRED_FIELD in _codes(findings)
    assert any("workspace_root" in f.detail for f in findings)


def test_clean_seed_passes_admission(tmp_path: Path) -> None:
    seed = _write(
        tmp_path,
        "---\nworkspace_root: /x\nstate_dir_relative: state/\nwork_registry: r.md\n"
        "completion_predicate:\n  - name: registry_drained\n---\n# body\n",
    )
    findings = list(V.validate_seed({"seed_path": seed}))
    assert findings == []  # no SEVERE finding -> admission proceeds
