"""The comparison of a notebook's existing manifest with a freshly computed one, and carrying the
existing manifest's dates forward into the fresh one where nothing changed."""
from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple, TypeVar

from steady_py.results import Delta, FieldChange, PackageChange

if TYPE_CHECKING:
    from steady_py import models

E = TypeVar("E", "models.ManifestPackage", "models.LocalModule", "models.Declaration")

# Top-level fields compared on their own terms below; every other top-level field is compared whole.
_SPECIAL_FIELDS = {"generated_at", "dependency_hash", "python_version", "gpu", "baseline", "packages", "local_modules",
                   "declarations"}


def _python_label(python_version: Dict[str, int]) -> str:
    return f"{python_version.get('major')}.{python_version.get('minor')}"


def _by_key(entries: Sequence[E]) -> Dict[Tuple[str, Tuple[str, ...]], E]:
    """Entries by their matching key; for a repeated key the first wins, as Cell 2 would install it."""
    out: Dict[Tuple[str, Tuple[str, ...]], E] = {}
    for entry in entries:
        out.setdefault(entry.key, entry)
    return out


def _entry_changes(section: str, before: Sequence[E], after: Sequence[E]) -> Tuple[List[E], List[E], List[FieldChange]]:
    """(added, removed, field changes) between two lists of manifest entries matched by key. Names
    are compared normalized (CH2); the display name and change date never make a change."""
    old, new = _by_key(before), _by_key(after)
    added = [new[k] for k in new if k not in old]
    removed = [old[k] for k in old if k not in new]
    changes: List[FieldChange] = []
    for key in (k for k in new if k in old):
        was, now = old[key].content(), new[key].content()
        changes += [FieldChange(section, new[key].label, name, was.get(name), now.get(name))
                    for name in sorted(set(was) | set(now)) if was.get(name) != now.get(name)]
    return added, removed, changes


def compute_delta(before: models.SteadyPyManifest, after: models.SteadyPyManifest) -> Delta:
    """What replacing `before` with `after` would change, field by field (P3).

    Packages are reported as added, removed or at a new version; every other change to a package,
    a local module or a declaration is a field change, as is a change to any other top-level field
    (the tool version, say). The baseline is compared whole: its findings and the packages that
    could not be checked.
    """
    added, removed, changes = _entry_changes("packages", before.packages, after.packages)
    version_changes = [
        PackageChange(c.name, c.before, c.after) for c in changes if c.field == "version"
    ]
    delta = Delta(
        added=[PackageChange(p.label, new_version=p.version) for p in sorted(added, key=lambda p: p.key)],
        removed=[PackageChange(p.label, old_version=p.version) for p in sorted(removed, key=lambda p: p.key)],
        version_changes=sorted(version_changes, key=lambda c: c.name.lower()),
        field_changes=[c for c in changes if c.field != "version"],
    )
    for section in ("local_modules", "declarations"):
        gained, lost, moved = _entry_changes(section, getattr(before, section), getattr(after, section))
        delta.field_changes += [FieldChange(section, e.label, "entry", None, e.content()) for e in gained]
        delta.field_changes += [FieldChange(section, e.label, "entry", e.content(), None) for e in lost]
        delta.field_changes += moved

    was, now = before.to_dict(), after.to_dict()
    for name in sorted((set(was) | set(now)) - _SPECIAL_FIELDS):
        if was.get(name) != now.get(name):
            delta.field_changes.append(FieldChange("manifest", "", name, was.get(name), now.get(name)))

    if before.python_version != after.python_version:
        delta.python_version = (_python_label(before.python_version), _python_label(after.python_version))
    if before.gpu != after.gpu:
        delta.gpu = (before.gpu, after.gpu)
    if after.baseline is not None:
        delta.baseline_compared = True
        old_found = set(before.baseline.findings) if before.baseline is not None else set()
        delta.findings_appeared = sorted(set(after.baseline.findings) - old_found)
        delta.findings_resolved = sorted(old_found - set(after.baseline.findings))
        old_errors = set(before.baseline.errors) if before.baseline is not None else set()
        delta.unchecked_appeared = sorted(set(after.baseline.errors) - old_errors)
        delta.unchecked_resolved = sorted(old_errors - set(after.baseline.errors))
    elif before.baseline is not None:  # a fresh manifest without a baseline drops the recorded one
        delta.field_changes.append(FieldChange("manifest", "", "baseline", was.get("baseline"), None))
    return delta


def _carried(previous: Sequence[E], fresh: Sequence[E], now: str) -> List[E]:
    """`fresh` with each entry's change date (and a package's display name) carried from its match
    in `previous` when its content is unchanged, and `now` otherwise."""
    old = _by_key(previous)
    out: List[E] = []
    for entry in fresh:
        entry = copy.copy(entry)
        match: Optional[Any] = old.get(entry.key)
        if match is not None and match.content() == entry.content():
            entry.changed_at = match.changed_at
            if hasattr(entry, "display_name"):
                entry.display_name = match.display_name
        else:
            entry.changed_at = now
        out.append(entry)
    return out


def settle(previous: Optional[models.SteadyPyManifest], fresh: models.SteadyPyManifest, now: str) -> models.SteadyPyManifest:
    """`fresh` dated against the manifest it replaces: each entry keeps its change date while its
    content is unchanged, and `generated_at` is when the manifest's content last changed, so a
    snapshot that changes nothing reproduces the previous manifest exactly (P5). Without a previous
    manifest everything is dated `now`."""
    settled = copy.copy(fresh)
    settled.packages = _carried(previous.packages if previous else [], fresh.packages, now)
    settled.local_modules = _carried(previous.local_modules if previous else [], fresh.local_modules, now)
    settled.declarations = _carried(previous.declarations if previous else [], fresh.declarations, now)
    unchanged = previous is not None and not compute_delta(previous, settled).has_changes
    settled.generated_at = previous.generated_at if unchanged and previous is not None else now
    return settled
