"""Local sibling modules: whether an import resolves to a file next to the notebook or under the
repository root rather than to a package, and the drift check that those files are still present."""
import importlib.util
import logging
from pathlib import Path
from typing import List, NamedTuple, Optional

from steady_py import util
from steady_py.constants import Severity, Signal
from steady_py.models import DriftFinding, SteadyPyManifest

logger = logging.getLogger("steady_py.localmodules")


class LocalModuleContext(NamedTuple):
    """Carries the two directories local-sibling-module resolution may check against."""
    notebook_dir: Optional[str] = None
    root_dir: Optional[str] = None


def _found_in_dir(name: str, directory: Optional[str]) -> bool:
    """Checks whether `name` resolves as a top-level module/package inside `directory`,
    without executing any code (top-level find_spec never runs __init__.py)."""
    if not directory or not Path(directory).exists():
        return False
    try:
        return importlib.machinery.PathFinder.find_spec(name, path=[directory]) is not None
    except Exception as e:
        logger.debug(f"[ModuleScan] find_spec probe failed for '{name}' in '{directory}': {e}")
        return False


@util.memoize_for_run
def resolve_local_module(name: str, notebook_dir: Optional[str], root_dir: Optional[str] = None) -> Optional[str]:
    """
    Checks whether `name` resolves as a local sibling module, using the most
    accurate mechanism available for how this process is running:

    - Live IPython/Jupyter kernel (util.is_running_in_ipython() True): the tool's own
      process IS (or is running inside) a real, already-verified environment, so
      this asks the live interpreter directly via an unrestricted find_spec --
      real sys.path, no guessing, correctly reflects platform-injected paths
      (Databricks Repos root, PYTHONPATH, editable installs, etc.).
    - External CLI/batch invocation: no live kernel exists for the target
      notebook, so this checks only the two directories that are actually
      knowable from outside -- the notebook's own directory and an optional
      declared root_dir -- via PathFinder, without executing any code and
      without mutating sys.path.

    Returns the anchor it resolved against ("notebook_dir" or "root_dir"), or
    None if not found by either. Only ever checks the top-level segment of a
    dotted name, matching how import classification already operates elsewhere
    in this file, and consistent with never executing package __init__ code.
    """
    top_level = name.split(".", 1)[0]

    if util.is_running_in_ipython():
        try:
            spec = importlib.util.find_spec(top_level)
        except Exception as e:
            logger.debug(f"[ModuleScan] live find_spec failed for '{top_level}': {e}")
            spec = None
        if spec is None:
            return None
        if _found_in_dir(top_level, notebook_dir):
            return "notebook_dir"
        return "root_dir"

    if _found_in_dir(top_level, notebook_dir):
        return "notebook_dir"
    if _found_in_dir(top_level, root_dir):
        return "root_dir"
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
        name = entry.get("name")
        anchor = entry.get("anchor")
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

        if _found_in_dir(name, anchor_dir):
            continue

        findings.append(DriftFinding(
            package=name, version="", signal=Signal.LOCAL_MODULE_MISSING, severity=Severity.CONFIRMED,
            message=f"'{name}' was originally found at {anchor_dir}; it can no longer be found there. "
                    f"Check that it will still be available to users, or re-run generation if the "
                    f"project structure changed.",
        ))

    return findings

