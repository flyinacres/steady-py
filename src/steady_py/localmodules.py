"""Local modules: the folders a notebook's own modules are found in (beside the notebook, or under
the project root), and the drift check that those files are still present. Whether an import is a
local module at all is the import join's decision (join.py)."""
import importlib.machinery
import logging
from pathlib import Path
from typing import List, Optional

from steady_py.constants import Severity, Signal
from steady_py.models import DriftFinding, SteadyPyManifest

logger = logging.getLogger("steady_py.localmodules")


# Files or folders that mark a project's root folder.
PROJECT_MARKERS = (".git", "pyproject.toml", "setup.py", "setup.cfg")


def project_root(notebook_dir: Optional[str], fallback: Optional[str]) -> Optional[str]:
    """The root a notebook's root-anchored local modules are found and re-verified under, the one
    derivation every entry point uses: the nearest folder at or above the notebook's that holds a
    project marker, so a file run, a directory run and check agree on it; `fallback` (the
    directory a run was given, or None) when there is none."""
    if notebook_dir:
        start = Path(notebook_dir).resolve()
        for folder in (start, *start.parents):
            if any((folder / marker).exists() for marker in PROJECT_MARKERS):
                return str(folder)
    return fallback


def spec_in_dir(name: str, directory: Optional[str]) -> Optional[importlib.machinery.ModuleSpec]:
    """How top-level `name` resolves inside `directory` alone, without running any code
    (top-level find_spec never runs __init__.py); None when it doesn't."""
    if not directory or not Path(directory).is_dir():
        return None
    try:
        return importlib.machinery.PathFinder.find_spec(name, path=[directory])
    except (ImportError, ValueError) as e:
        logger.debug(f"[ModuleScan] find_spec probe failed for '{name}' in '{directory}': {e}")
        return None


def check_local_modules(
    manifest: SteadyPyManifest, notebook_dir: Optional[str], root_dir: Optional[str] = None
) -> List[DriftFinding]:
    """Re-verifies each local module recorded at generation time by plain
    filesystem existence -- never by import resolution, since drift-check must
    give the same answer regardless of the environment it happens to run in.

    Three outcomes per entry:
    - still found at its recorded anchor -> no finding.
    - anchor directory itself is gone (or a root_dir-anchored entry has no
      root_dir supplied here) -> "error" severity: genuinely unverifiable,
      not necessarily broken (the project may have just moved).
    - anchor directory intact but this specific name is gone -> "confirmed"
      severity: a real, specific finding.
    """
    findings: List[DriftFinding] = []

    for entry in manifest.local_modules:
        name, anchor = entry.name, entry.anchor
        if not name:
            continue

        if anchor == "root_dir":
            if root_dir is None:
                findings.append(DriftFinding(
                    package=name, version="", signal=Signal.LOCAL_MODULE_UNVERIFIABLE, severity=Severity.ERROR,
                    message=f"'{name}' was recorded via a root_dir at generation time; none was supplied "
                            f"for this check, so it can't be verified.",
                ))
                continue
            anchor_dir: Optional[str] = root_dir
        else:
            anchor_dir = notebook_dir

        if not anchor_dir or not Path(anchor_dir).exists():
            findings.append(DriftFinding(
                package=name, version="", signal=Signal.LOCAL_MODULE_UNVERIFIABLE, severity=Severity.ERROR,
                message=f"Cannot verify '{name}': the recorded location's directory no longer exists. "
                        f"If the project was moved, re-run generation to update.",
            ))
            continue

        if spec_in_dir(name, anchor_dir) is not None:
            continue

        findings.append(DriftFinding(
            package=name, version="", signal=Signal.LOCAL_MODULE_MISSING, severity=Severity.CONFIRMED,
            message=f"'{name}' was originally found at {anchor_dir}; it can no longer be found there. "
                    f"Check that it will still be available to users, or re-run generation if the "
                    f"project structure changed."
                    + (" If the project's root folder is elsewhere, pass it with --root-dir." if anchor == "root_dir" else ""),
        ))

    return findings

