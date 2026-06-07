"""FUP-0855 — production candidate enrichment (supervisor.candidate_enrichment).

Verifies the seam that derives the §6 Admission Gate inputs from a candidate's
seed on disk, replacing the no-op identity default so a live discover -> admit
cycle is fed a complete row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor.candidate_enrichment import (
    WORKSPACE_ROOT_ENV,
    default_candidate_enricher,
    enrich_candidate_from_seed,
    make_seed_candidate_enricher,
    open_work_counts_for,
)

pytestmark = pytest.mark.unit


_SEED = """---
initiative:
  slug: demo_proj
  project_id: demo_proj
workspace_root: "{ws}"
work_registry: "Demo_Register.md"
read_only_paths:
  - "{ws}/corpus"
mcp_servers:
  - name: filesystem
    command: npx
    args:
      - "-y"
      - "@modelcontextprotocol/server-filesystem"
      - "{proj}"
      - "{ws}/shared"
  - name: supabase
    command: npx
    args:
      - "-y"
      - "@supabase/mcp-server-supabase@latest"
      - "--project-ref=abc123"
---

# Demo seed body (ignored by the enricher).
"""

_REGISTER = """# Demo Register

Per the registry_zero_open contract: open Priority `**P1/P2/P3**`, closed `**RESOLVED**`.

| ID | Name | Gap | Priority | Prereq | Resolution |
|---|---|---|---|---|---|
| D-01 | a | x | **RESOLVED** (2026-06-06, done) | - | - |
| D-02 | b | x | **P1** | - | - |
| D-03 | c | x | **P2** | - | - |
| D-04 | d | x | **RESOLVED** (2026-06-06, done) | - | - |
| D-05 | e | x | **P3** | - | - |
"""


def _make_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Build a workspace with one candidate project (folder + seed + register)."""
    ws = tmp_path / "Project_Docs"
    proj = ws / "Sub_Projects" / "demo-proj"
    proj.mkdir(parents=True)
    (proj / "Demo_Project_Seed_v1.0.md").write_text(
        _SEED.format(ws=ws.as_posix(), proj=proj.as_posix()), encoding="utf-8"
    )
    # work_registry resolves scan-newest under the workspace root.
    (ws / "Demo_Register.md").write_text(_REGISTER, encoding="utf-8")
    return ws, proj


def test_enriches_all_admission_inputs_from_seed(tmp_path: Path) -> None:
    ws, proj = _make_workspace(tmp_path)
    row = {"project_id": "demo_proj", "folder_path": "Sub_Projects/demo-proj"}

    out = enrich_candidate_from_seed(row, workspace_root=ws)

    assert out["seed_path"].endswith("Demo_Project_Seed_v1.0.md")
    assert out["initiative_slug"] == "demo_proj"
    assert out["open_item_count"] == 3  # D-02 / D-03 / D-05
    assert out["read_only_paths"] == [f"{ws.as_posix()}/corpus"]
    # No explicit writable_paths in the seed -> the project dir is the writable root
    # (derived via str(folder_path), so OS-native separators).
    assert out["writable_paths"] == [str(proj)]
    # filesystem MCP path args become mcp_roots verbatim (the package spec + flags are
    # dropped); the seed wrote them as_posix, so compare against the posix forms.
    assert out["mcp_roots"] == [proj.as_posix(), f"{ws.as_posix()}/shared"]
    # The original projects columns survive.
    assert out["project_id"] == "demo_proj"


def test_absolute_folder_path_is_used_as_is(tmp_path: Path) -> None:
    ws, proj = _make_workspace(tmp_path)
    row = {"project_id": "demo_proj", "folder_path": str(proj)}

    out = enrich_candidate_from_seed(row, workspace_root=ws)

    assert out["seed_path"].endswith("Demo_Project_Seed_v1.0.md")
    assert out["open_item_count"] == 3


def test_absolute_work_registry_is_resolved_directly(tmp_path: Path) -> None:
    """Regression: a seed declaring an ABSOLUTE ``work_registry`` (as the real
    oltest_c2 harness does) must be read directly — NOT passed to ``Path.glob`` as a
    pattern, which raises ``NotImplementedError`` on a non-relative pattern and
    previously crashed the whole schedule step / ``python -m supervisor``."""
    ws, proj = _make_workspace(tmp_path)
    abs_register = (ws / "Demo_Register.md")
    seed = _SEED.replace(
        'work_registry: "Demo_Register.md"',
        f'work_registry: "{abs_register.as_posix()}"',
    )
    (proj / "Demo_Project_Seed_v1.0.md").write_text(
        seed.format(ws=ws.as_posix(), proj=proj.as_posix()), encoding="utf-8"
    )
    row = {"project_id": "demo_proj", "folder_path": str(proj)}

    out = enrich_candidate_from_seed(row, workspace_root=ws)  # must not raise

    assert out["open_item_count"] == 3  # D-02 / D-03 / D-05, read from the absolute path


def test_absolute_work_registry_missing_file_omits_count(tmp_path: Path) -> None:
    """An absolute ``work_registry`` pointing at a non-existent file degrades to
    'omit open_item_count' (never raises), so admission safely refuses rather than
    the cycle crashing."""
    ws, proj = _make_workspace(tmp_path)
    seed = _SEED.replace(
        'work_registry: "Demo_Register.md"',
        f'work_registry: "{(ws / "Nope.md").as_posix()}"',
    )
    (proj / "Demo_Project_Seed_v1.0.md").write_text(
        seed.format(ws=ws.as_posix(), proj=proj.as_posix()), encoding="utf-8"
    )
    out = enrich_candidate_from_seed(
        {"project_id": "demo_proj", "folder_path": str(proj)}, workspace_root=ws
    )
    assert "open_item_count" not in out  # omitted, not a false 0; no crash


def test_absolute_corpus_path_also_surfaces_bare_token(tmp_path: Path) -> None:
    """FR-034: when a seed declares the corpus by its absolute path, the enricher also
    surfaces the canonical bare READ_ONLY_CORPUS_PATH token so admission's gate
    recognizes it — the belt-and-suspenders half of the assembled-run FR-034 fix."""
    from supervisor.safety_gates import READ_ONLY_CORPUS_PATH

    ws, proj = _make_workspace(tmp_path)
    # Forward-slash absolute form (YAML-safe; the enricher normalizes separators).
    abs_corpus = "K:/Claude Code Factory/V3/Project_Docs/Project_Docs_Current/"
    seed = _SEED.replace('- "{ws}/corpus"', f'- "{abs_corpus}"')
    (proj / "Demo_Project_Seed_v1.0.md").write_text(
        seed.format(ws=ws.as_posix(), proj=proj.as_posix()), encoding="utf-8"
    )
    out = enrich_candidate_from_seed(
        {"project_id": "demo_proj", "folder_path": str(proj)}, workspace_root=ws
    )
    assert abs_corpus in out["read_only_paths"]
    assert READ_ONLY_CORPUS_PATH in out["read_only_paths"]  # bare token added


def test_non_corpus_read_only_path_is_left_alone(tmp_path: Path) -> None:
    """A read-only path that does NOT resolve to the corpus dir is not augmented."""
    from supervisor.safety_gates import READ_ONLY_CORPUS_PATH

    ws, proj = _make_workspace(tmp_path)
    out = enrich_candidate_from_seed(
        {"project_id": "demo_proj", "folder_path": str(proj)}, workspace_root=ws
    )
    # _SEED declares read_only "{ws}/corpus" (not the corpus dir) -> token NOT added.
    assert out["read_only_paths"] == [f"{ws.as_posix()}/corpus"]
    assert READ_ONLY_CORPUS_PATH not in out["read_only_paths"]


def test_passthrough_when_no_workspace_root(tmp_path: Path) -> None:
    row = {"project_id": "demo_proj", "folder_path": "Sub_Projects/demo-proj"}
    assert enrich_candidate_from_seed(row, workspace_root=None) == row


def test_passthrough_when_no_seed_found(tmp_path: Path) -> None:
    ws = tmp_path / "Project_Docs"
    (ws / "Sub_Projects" / "empty-proj").mkdir(parents=True)
    row = {"project_id": "x", "folder_path": "Sub_Projects/empty-proj"}
    assert enrich_candidate_from_seed(row, workspace_root=ws) == row


def test_never_overwrites_existing_keys(tmp_path: Path) -> None:
    ws, _ = _make_workspace(tmp_path)
    row = {
        "project_id": "demo_proj",
        "folder_path": "Sub_Projects/demo-proj",
        "seed_path": "/already/set/seed.md",
        "open_item_count": 99,
    }

    out = enrich_candidate_from_seed(row, workspace_root=ws)

    # Caller-supplied values win; only the missing fields are filled in.
    assert out["seed_path"] == "/already/set/seed.md"
    assert out["open_item_count"] == 99
    assert out["read_only_paths"] == [f"{ws.as_posix()}/corpus"]


def test_never_raises_on_bad_row(tmp_path: Path) -> None:
    ws, _ = _make_workspace(tmp_path)
    # No folder_path, a non-string folder_path, and an empty row all pass through.
    assert enrich_candidate_from_seed({}, workspace_root=ws) == {}
    bad = {"folder_path": 123}
    assert enrich_candidate_from_seed(bad, workspace_root=ws) == bad


def test_factory_binds_workspace_root(tmp_path: Path) -> None:
    ws, _ = _make_workspace(tmp_path)
    enricher = make_seed_candidate_enricher(ws)
    out = enricher({"project_id": "demo_proj", "folder_path": "Sub_Projects/demo-proj"})
    assert out["open_item_count"] == 3


def test_open_work_counts_for_live_source(tmp_path: Path) -> None:
    ws, _ = _make_workspace(tmp_path)  # demo-proj registry has 3 open rows
    rows = [
        {"project_id": "demo_proj", "folder_path": "Sub_Projects/demo-proj"},
        {"project_id": "ghost", "folder_path": "Sub_Projects/does-not-exist"},  # omitted
        {"folder_path": "Sub_Projects/demo-proj"},  # no project_id → skipped
    ]
    counts = open_work_counts_for(rows, workspace_root=ws)
    assert counts == {"demo_proj": 3}  # only the resolvable project; FR-024 source


def test_open_work_counts_for_no_root_is_empty(tmp_path: Path) -> None:
    assert open_work_counts_for(
        [{"project_id": "p", "folder_path": "x"}], workspace_root=None
    ) == {}


def test_default_enricher_reads_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws, _ = _make_workspace(tmp_path)
    row = {"project_id": "demo_proj", "folder_path": "Sub_Projects/demo-proj"}

    # No env set -> pass-through.
    monkeypatch.delenv(WORKSPACE_ROOT_ENV, raising=False)
    assert default_candidate_enricher(dict(row)) == row

    # Env set -> enriches.
    monkeypatch.setenv(WORKSPACE_ROOT_ENV, str(ws))
    out = default_candidate_enricher(dict(row))
    assert out["open_item_count"] == 3
    assert out["seed_path"].endswith("Demo_Project_Seed_v1.0.md")
