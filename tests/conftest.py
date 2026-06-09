"""Pytest scaffold for the Outer Loop supervisor build (ol-build).

The supervisor package is authored under ../supervisor by the build
(OLB-01 onward). This conftest makes the repo root importable so the
package resolves as `supervisor.<module>`, and provides the common
fixtures that component_build / integration_checkpoint tests rely on.
"""
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SUPERVISOR = _REPO_ROOT / "supervisor"

# Repo root ONLY. supervisor/ is a package (has __init__.py) → import it as
# `supervisor.<module>`. Do NOT add _SUPERVISOR to sys.path: putting the package
# directory itself on the path lets a module be imported two ways (bare `import foo`
# AND `import supervisor.foo`), which raises pytest's "imported twice under
# different names" error under --import-mode=importlib once OLB-01 lands code.
_sp = str(_REPO_ROOT)
if _sp not in sys.path:
    sys.path.insert(0, _sp)


@pytest.fixture
def repo_root() -> Path:
    """Absolute path to the Ralph-dev repo root."""
    return _REPO_ROOT


@pytest.fixture
def supervisor_dir() -> Path:
    """Absolute path to the supervisor package under build."""
    return _SUPERVISOR
