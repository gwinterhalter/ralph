"""FUP-0855: production candidate enrichment for the §6 Admission Gate.

``RegistryPort.read_candidates`` surfaces only the ``projects`` columns
(Spec v1.3 §5.2). The Admission Gate (:mod:`supervisor.admission`) additionally
reads seed-derived fields off each candidate row — ``seed_path`` (FR-021 spawn),
``open_item_count`` (FR-018 non-empty work registry), and the ``writable_paths`` /
``mcp_roots`` / ``read_only_paths`` / ``design_zone`` Blast-Radius inputs (FR-020).
The component tests bridged this by hand-enriching the discovered row (the C2/C3
``_enrich*`` helpers); this module is the **production** seam that derives those
fields from the candidate's seed on disk, so a live discover -> admit cycle is fed
a complete row instead of a bare ``projects`` row that admission would refuse.

Conventions (defaults mirror the build's own; all overridable):

* the project directory is ``projects.folder_path`` resolved against the
  supervisor's workspace root (the ``OL_SUPERVISOR_WORKSPACE_ROOT`` env, parallel
  to ``OL_SUPERVISOR_DB_URL``); an absolute ``folder_path`` is used as-is;
* the seed is the newest top-level file matching ``*[Ss]eed*.md`` in that
  directory (scan-newest — the build's anti-shadow convention);
* ``open_item_count`` is the number of OPEN gap rows in the seed's ``work_registry``
  (resolved scan-newest under the workspace root) — open per the ``registry_zero_open``
  contract (Priority cell ``**P1/P2/P3**``; ``**RESOLVED**`` = closed), the same count
  the orchestrator's ``stop_check`` uses;
* ``writable_paths`` defaults to the project directory; ``mcp_roots`` is derived
  from the seed's ``mcp_servers`` filesystem mount args; ``read_only_paths`` /
  ``design_zone`` come straight from the seed.

The enricher is **fault-tolerant**: any field it cannot derive is simply left off
the row (admission then safely refuses an under-populated candidate rather than
the cycle crashing). It NEVER raises and NEVER overwrites a key already present on
the row — so a caller-supplied / pre-enriched row (a checkpoint test, or a future
DB-backed candidate) is returned unchanged.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from supervisor.ports import RegistryRow
from supervisor.safety_gates import READ_ONLY_CORPUS_PATH

WORKSPACE_ROOT_ENV = "OL_SUPERVISOR_WORKSPACE_ROOT"

#: Normalized tail of the FR-034 corpus token, for matching a seed's read-only paths.
_CORPUS_TAIL = READ_ONLY_CORPUS_PATH.replace("\\", "/").rstrip("/").lower()

_SEED_GLOB = "*[Ss]eed*.md"
# An OPEN gap's Priority CELL, per the ``registry_zero_open`` contract the
# orchestrator's stop_check uses: a table cell whose content begins with the bold
# priority token ``**P1**``/``**P2**``/``**P3**`` (matched as ``|`` + whitespace +
# token, so it pins the Priority *cell* — not a prose ``**P1**`` mention inside a
# change-history / summary cell, which would over-count). A gap is CLOSED once its
# Priority cell begins ``**RESOLVED**``. This matches the real Ralph register format
# (the Priority-cell convention); the old status-in-column-2 heuristic counted 0
# against every real register, silently blocking live admission (FR-018).
_OPEN_ROW = re.compile(r"\|\s*\*\*P[123]\*\*", re.MULTILINE)
# YAML frontmatter = the block between the first two ``---`` fences.
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# The keys this module derives. None is ever written over an existing row value.
_DERIVED_KEYS = (
    "seed_path",
    "initiative_slug",
    "open_item_count",
    "writable_paths",
    "mcp_roots",
    "read_only_paths",
    "design_zone",
)


def _newest_match(directory: Path, glob: str) -> Path | None:
    """Newest top-level file in ``directory`` matching ``glob`` (None if none).

    Never raises: a non-relative / malformed pattern (``Path.glob`` rejects an
    absolute pattern with ``NotImplementedError``/``ValueError``) degrades to None
    rather than propagating — preserving the module's fault-tolerance contract.
    """
    try:
        matches = [p for p in directory.glob(glob) if p.is_file()]
    except (OSError, ValueError, NotImplementedError):
        return None
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def _parse_frontmatter(seed_text: str) -> dict[str, Any]:
    """Return the seed's YAML frontmatter as a dict ({} if absent/unparseable)."""
    match = _FRONTMATTER.search(seed_text)
    if match is None:
        return {}
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _filesystem_mcp_roots(front: dict[str, Any]) -> list[str]:
    """Path-like args of the ``filesystem`` MCP server (the mounted roots)."""
    servers = front.get("mcp_servers")
    if not isinstance(servers, Sequence):
        return []
    roots: list[str] = []
    for server in servers:
        if not isinstance(server, dict) or server.get("name") != "filesystem":
            continue
        for arg in server.get("args", []) or []:
            text = str(arg)
            # A path-like arg: a drive root or a separator, not a flag/package spec.
            if text.startswith(("-", "@")):
                continue
            if ":" in text or "\\" in text or "/" in text:
                roots.append(text)
    return roots


def _open_item_count(front: dict[str, Any], workspace_root: Path) -> int | None:
    """Count OPEN gap rows (``registry_zero_open`` contract) in the seed's
    ``work_registry`` (or None).

    ``work_registry`` is a bare filename resolved scan-newest under the workspace
    root — the orchestrator's resolution. Returns None when it cannot be resolved,
    so the field is omitted rather than asserting a false zero.
    """
    name = front.get("work_registry")
    if not isinstance(name, str) or not name:
        return None
    # ``work_registry`` may be a bare filename (resolved scan-newest under the
    # workspace root — the orchestrator's convention) OR an absolute path: real
    # seeds commonly declare the full path (e.g. the oltest_c2 harness). Resolve an
    # absolute value directly; only glob/rglob a bare name, because ``Path.glob``
    # raises on a non-relative pattern.
    name_path = Path(name)
    if name_path.is_absolute():
        registry_path: Path | None = name_path if name_path.is_file() else None
    else:
        registry_path = _newest_match(workspace_root, name)
        if registry_path is None:
            # Fall back to a recursive scan (the register may live in a subfolder).
            try:
                candidates = [p for p in workspace_root.rglob(name) if p.is_file()]
            except (OSError, ValueError, NotImplementedError):
                return None
            if not candidates:
                return None
            registry_path = max(candidates, key=lambda p: p.stat().st_mtime)
    if registry_path is None:
        return None
    try:
        text = registry_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return len(_OPEN_ROW.findall(text))


def _resolve_workspace_root(workspace_root: str | os.PathLike[str] | None) -> Path | None:
    root = workspace_root if workspace_root is not None else os.environ.get(
        WORKSPACE_ROOT_ENV
    )
    if not root:
        return None
    return Path(str(root))


def seed_hang_timeout_seconds(seed_path: str | os.PathLike[str]) -> float | None:
    """Read ``budget.hang_timeout_seconds`` from the seed at ``seed_path`` (or ``None``).

    The per-project stall budget the §4.4(1) Reconcile step honors (resolving review
    finding F-4: the field was declared in every seed but read nowhere). Fault-tolerant:
    returns ``None`` when the seed is unreadable, has no frontmatter, or declares no
    numeric value — the caller then falls back to the fleet-default hang timeout. Booleans
    are rejected (``isinstance(True, int)`` is True in Python). Never raises.
    """
    try:
        text = Path(seed_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    budget = _parse_frontmatter(text).get("budget")
    if not isinstance(budget, dict):
        return None
    value = budget.get("hang_timeout_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def enrich_candidate_from_seed(
    row: RegistryRow,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    seed_glob: str = _SEED_GLOB,
) -> RegistryRow:
    """Return ``row`` merged with the seed-derived §6 admission inputs.

    Best-effort and side-effect-free: reads only the candidate's seed + work
    registry. Returns the row unchanged when the workspace root is unknown, the
    project directory or seed cannot be located, or a field cannot be derived.
    An existing key on ``row`` is never overwritten.
    """
    root = _resolve_workspace_root(workspace_root)
    if root is None:
        return row

    folder = row.get("folder_path")
    if not isinstance(folder, str) or not folder:
        return row
    folder_path = Path(folder)
    if not folder_path.is_absolute():
        folder_path = root / folder_path

    seed_file = _newest_match(folder_path, seed_glob)
    if seed_file is None:
        return row
    try:
        seed_text = seed_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return row
    front = _parse_frontmatter(seed_text)

    derived: dict[str, object] = {"seed_path": str(seed_file)}

    initiative = front.get("initiative")
    if isinstance(initiative, dict):
        slug = initiative.get("slug") or initiative.get("project_id")
        if slug:
            derived["initiative_slug"] = str(slug)

    read_only = front.get("read_only_paths")
    if isinstance(read_only, Sequence) and not isinstance(read_only, (str, bytes)):
        ro_paths = [str(p) for p in read_only]
        # FR-034: real seeds declare the corpus by its ABSOLUTE path, but the OLB-06
        # gate constant is the location-agnostic bare token. When a declared path
        # resolves to the corpus dir, also surface the bare token so admission's
        # read-only invariant recognizes it however the seed spelled the path. (The
        # gate matcher accepts the absolute form too — this is the belt-and-suspenders
        # half so the canonical token is present regardless of route.)
        resolves_to_corpus = any(
            p.replace("\\", "/").rstrip("/").lower().endswith(_CORPUS_TAIL) for p in ro_paths
        )
        if resolves_to_corpus and READ_ONLY_CORPUS_PATH not in ro_paths:
            ro_paths.append(READ_ONLY_CORPUS_PATH)
        derived["read_only_paths"] = ro_paths

    writable = front.get("writable_paths")
    if isinstance(writable, Sequence) and not isinstance(writable, (str, bytes)):
        derived["writable_paths"] = [str(p) for p in writable]
    else:
        # No explicit field — the project's own directory is its writable root.
        derived["writable_paths"] = [str(folder_path)]

    mcp_roots = front.get("mcp_roots")
    if isinstance(mcp_roots, Sequence) and not isinstance(mcp_roots, (str, bytes)):
        derived["mcp_roots"] = [str(p) for p in mcp_roots]
    else:
        fs_roots = _filesystem_mcp_roots(front)
        if fs_roots:
            derived["mcp_roots"] = fs_roots

    design_zone = front.get("design_zone")
    if isinstance(design_zone, str) and design_zone:
        derived["design_zone"] = design_zone

    open_count = _open_item_count(front, root)
    if open_count is not None:
        derived["open_item_count"] = open_count

    # Never overwrite a value the row already carries (pre-enriched / caller-set).
    merged = dict(row)
    for key, value in derived.items():
        merged.setdefault(key, value)
    return merged


def open_work_counts_for(
    rows: "Sequence[RegistryRow]",
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    seed_glob: str = _SEED_GLOB,
) -> dict[str, int]:
    """Live FR-024 open-work-count source: project_id -> open registry rows (D2).

    For each project row (carrying ``project_id`` + ``folder_path``), locates its
    seed and counts the OPEN gap rows (``registry_zero_open``) in that seed's
    ``work_registry`` —
    the same count the orchestrator's ``stop_check`` uses. The result feeds the
    scheduler's FR-024 closest-to-done bias and the §13 status surface, replacing the
    supplied-parameter placeholder with a live read. Fault-tolerant: a project whose
    workspace/seed/registry cannot be resolved is simply omitted (the consumer treats
    a missing project as 0), and the function never raises.
    """
    root = _resolve_workspace_root(workspace_root)
    counts: dict[str, int] = {}
    if root is None:
        return counts
    for row in rows:
        project_id = row.get("project_id")
        folder = row.get("folder_path")
        if not isinstance(project_id, str) or not project_id:
            continue
        if not isinstance(folder, str) or not folder:
            continue
        folder_path = Path(folder)
        if not folder_path.is_absolute():
            folder_path = root / folder_path
        seed_file = _newest_match(folder_path, seed_glob)
        if seed_file is None:
            continue
        try:
            front = _parse_frontmatter(
                seed_file.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
        count = _open_item_count(front, root)
        if count is not None:
            counts[project_id] = count
    return counts


def make_seed_candidate_enricher(
    workspace_root: str | os.PathLike[str] | None = None,
    *,
    seed_glob: str = _SEED_GLOB,
) -> Callable[[RegistryRow], RegistryRow]:
    """Bind a workspace root into a one-arg enricher for ``ScheduleConfig``.

    With ``workspace_root=None`` the returned enricher reads
    ``OL_SUPERVISOR_WORKSPACE_ROOT`` at call time.
    """

    def _enricher(row: RegistryRow) -> RegistryRow:
        return enrich_candidate_from_seed(
            row, workspace_root=workspace_root, seed_glob=seed_glob
        )

    return _enricher


def default_candidate_enricher(row: RegistryRow) -> RegistryRow:
    """The ``ScheduleConfig`` default: enrich from the seed when a workspace root is
    configured (``OL_SUPERVISOR_WORKSPACE_ROOT``), otherwise pass the row through.

    This makes a production cycle that exports the workspace root (alongside
    ``OL_SUPERVISOR_DB_URL``) get seed enrichment automatically, while a test or a
    cycle with no configured root — or one that supplies its own enricher — is
    unaffected.
    """
    return enrich_candidate_from_seed(row)


__all__ = [
    "WORKSPACE_ROOT_ENV",
    "enrich_candidate_from_seed",
    "open_work_counts_for",
    "seed_hang_timeout_seconds",
    "make_seed_candidate_enricher",
    "default_candidate_enricher",
]
