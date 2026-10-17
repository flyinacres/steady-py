"""The comparison of a notebook's existing manifest with a freshly computed one."""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List

from steady_py.results import Delta, PackageChange

if TYPE_CHECKING:
    from steady_py import models


def _python_label(python_version: Dict[str, int]) -> str:
    return f"{python_version.get('major')}.{python_version.get('minor')}"


def compute_delta(before: models.SteadyPyManifest, after: models.SteadyPyManifest) -> Delta:
    """What replacing `before` with `after` would change.

    Packages are matched by their recorded name, extras included. The baseline is compared only
    when `after` has one: scan builds its fresh manifest without contacting PyPI, so it has none.
    """
    old = {dep.name: dep.version for dep in before.dependencies}
    new = {dep.name: dep.version for dep in after.dependencies}

    delta = Delta(
        added=[PackageChange(name, new_version=new[name]) for name in sorted(new.keys() - old.keys())],
        removed=[PackageChange(name, old_version=old[name]) for name in sorted(old.keys() - new.keys())],
        version_changes=[PackageChange(name, old[name], new[name]) for name in sorted(old.keys() & new.keys()) if old[name] != new[name]],
    )
    if before.python_version != after.python_version:
        delta.python_version = (_python_label(before.python_version), _python_label(after.python_version))
    if before.gpu != after.gpu:
        delta.gpu = (before.gpu, after.gpu)
    if after.baseline is not None:
        delta.baseline_compared = True
        was = set(before.baseline.findings) if before.baseline is not None else set()
        now = set(after.baseline.findings)
        appeared: List[models.FindingKey] = sorted(now - was)
        resolved: List[models.FindingKey] = sorted(was - now)
        delta.findings_appeared, delta.findings_resolved = appeared, resolved
    return delta
