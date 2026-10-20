"""Install-line reconciliation: which of a notebook's install lines can undo what Cell 2 installed
(conflicting) and which repeat it (redundant). One pure function classifies, shared by snapshot and
check (review_findings.md, install-lines spec, items 5 and 10)."""
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion

from steady_py.constants import DECLARATIONS_NAME, DependencyStatus, Readability, Severity, Signal, TargetKind, Tool
from steady_py.models import DiagnosticEvent, DriftFinding, InstallLine, InstallTarget, ManifestPackage

_REINSTALL = {"--force-reinstall", "--ignore-installed", "--reinstall"}
_EGG = re.compile(r"#egg=([A-Za-z0-9._-]+)")


@dataclass
class LineVerdict:
    """One install line's effect on one package Cell 2 installs."""
    line: InstallLine
    package: ManifestPackage
    conflicting: bool
    reason: str  # why a conflicting line can change the package; empty for a redundant one


def _named(target: InstallTarget) -> Optional[str]:
    """The canonical package a target names, when the line says which."""
    if target.kind in (TargetKind.REQUIREMENT, TargetKind.DIRECT_REFERENCE) and target.canonical:
        return target.canonical
    if target.kind == TargetKind.NAME:  # a conda package, parsed no further than its name (`numpy=1.26` too)
        name = re.split(r"[=<>!~\s]", (target.name or target.text).split("::")[-1])[0]
        return canonicalize_name(name) if name else None
    if target.kind == TargetKind.EDITABLE:
        egg = _EGG.search(target.text)
        return canonicalize_name(egg.group(1)) if egg else None
    return None


def _excludes(specifier: str, version: str) -> bool:
    try:
        return not SpecifierSet(specifier).contains(version, prereleases=True)
    except (InvalidSpecifier, InvalidVersion):
        return False  # unreadable: not classified as a conflict on that ground


def _exactly(specifier: str, version: str) -> bool:
    """Whether the specifier admits only `version`."""
    clauses = [c.strip() for c in specifier.split(",") if c.strip()]
    return len(clauses) == 1 and clauses[0].lstrip("=").strip() == version and clauses[0].startswith("==") \
        and "*" not in clauses[0]


def _verdict(line: InstallLine, target: InstallTarget, package: ManifestPackage) -> Optional[str]:
    """Why the line can leave `package` at another version or distribution, or None if it can't."""
    names = {o.name for o in line.options}
    values = {o.name: canonicalize_name(o.value) for o in line.options if o.value}
    if target.kind == TargetKind.NAME:
        return "is a conda-family install, and conda ignores what pip installed"
    if target.kind == TargetKind.EDITABLE:
        return "installs it in editable mode from elsewhere"
    if names & _REINSTALL or values.get("--reinstall-package") == package.name:
        return "reinstalls it, and its dependencies with it"
    if target.kind == TargetKind.DIRECT_REFERENCE:
        if package.status != DependencyStatus.DIRECT_REFERENCE:
            return "installs it from a direct source instead of the pinned release"
        return None if target.text.split("@", 1)[-1].strip() == (package.url or "") else "installs it from a different source"
    version = package.version
    if version is None:
        return None  # a direct source with no version: only a reinstall or another source can move it
    if target.specifier and _excludes(target.specifier, version):
        return f"asks for {target.specifier}, which excludes {version}"
    upgrade = "--upgrade" in names or values.get("--upgrade-package") == package.name
    if upgrade and not _exactly(target.specifier, version):
        return "upgrades it whenever a newer release exists"
    return None


def classify(lines: Sequence[InstallLine], packages: Sequence[ManifestPackage]) -> List[LineVerdict]:
    """Each literal kernel install line naming a package Cell 2 installs, as conflicting or redundant.
    Computed or unreadable lines, `-r` files and lines that install elsewhere aren't classified, and
    neither are ignored packages, since an ignore keeps them out of the pins; a guard doesn't change
    the classification."""
    installs: Dict[str, ManifestPackage] = {}
    for package in packages:
        if package.installable:
            installs.setdefault(package.name, package)
    verdicts: List[LineVerdict] = []
    for line in lines:
        if not line.kernel_target or line.readability != Readability.LITERAL or line.tool == Tool.SYSTEM:
            continue
        for target in line.targets:
            name = _named(target)
            if name is None or name not in installs:
                continue
            reason = _verdict(line, target, installs[name])
            verdicts.append(LineVerdict(line, installs[name], conflicting=reason is not None, reason=reason or ""))
    return verdicts


# --- what each entry point says -----------------------------------------------------------------

def _quoted(line: InstallLine) -> str:
    """The line as the creator finds it: its text, with the nearest heading as a hint (item 5.6)."""
    heading = f' (under "{line.cell.heading}")' if line.cell.heading else ""
    return f"`{line.text}`{heading}"


def _install_entry(package: ManifestPackage) -> str:
    source = f'"url": "{package.url}"' if package.status == DependencyStatus.DIRECT_REFERENCE else f'"version": "{package.version}"'
    return f'{{"install": "{package.name}", {source}, "reason": "..."}}'


def _advice(verdict: LineVerdict, imported: Set[str], companion: bool) -> str:
    """What to do with the line (item 5.2): delete it when an import keeps the pin alive, else replace
    it with an install declaration. A guarded line runs only sometimes, so it is never just deleted."""
    package = verdict.package
    if verdict.line.guard is not None:
        return (f'It runs only when its condition holds. If the notebook needs that, add {{"ignore": "{package.name}", '
                f'"reason": "..."}} to {DECLARATIONS_NAME} so Cell 2 leaves it alone; otherwise delete the line.')
    shared = len(verdict.line.targets) > 1  # the line installs other packages too: only this one goes
    remove = f"Remove {package.name} from the line" if shared else "Delete the line"
    if package.name in imported:
        note = " Once it's deleted, the source notebook installs nothing without the companion." if companion and not shared else ""
        return f"{remove}; the import keeps the pin.{note}"
    return (f"{remove} and add {_install_entry(package)} to {DECLARATIONS_NAME}, "
            f"so the pin doesn't depend on this line.")


def snapshot_diagnostics(verdicts: Sequence[LineVerdict], imported: Set[str], companion: bool) -> List[DiagnosticEvent]:
    """Snapshot's advice, one diagnostic per line and package: a conflicting line is a warning that
    sets exit 1 unless accepted (item 10.1), a redundant one a notice."""
    out = []
    for v in verdicts:
        package, advice = v.package, _advice(v, imported, companion)
        if v.conflicting:
            event = DiagnosticEvent.at(v.line.cell, v.line.line_idx, "conflicting_install_line", (
                f"{_quoted(v.line)} can change {package.label} after Cell 2 installs {package.requirement()}: it {v.reason}. "
                f'{advice} To keep the line as it is, add {{"accept": "{package.name}", "reason": "..."}}.'))
        else:
            event = DiagnosticEvent.at(v.line.cell, v.line.line_idx, "redundant_install_line", (
                f"{_quoted(v.line)} repeats {package.requirement()}, which Cell 2 installs, and changes nothing. {advice}"),
                level="notice")
        event.names = (package.name,)
        out.append(event)
    return out


def check_findings(verdicts: Sequence[LineVerdict]) -> List[DriftFinding]:
    """Check's view (item 10.2): a conflicting line is a confirmed finding, a redundant one a notice.
    Check gives no delete-or-replace advice, since that needs the import join and so the environment."""
    return [DriftFinding(
        package=v.package.name, version=v.package.version or "",
        signal=Signal.CONFLICTING_LINE if v.conflicting else Signal.REDUNDANT_LINE,
        severity=Severity.CONFIRMED if v.conflicting else Severity.NOTICE,
        message=(f"{_quoted(v.line)} can change {v.package.label} after Cell 2 installs {v.package.requirement()}: it {v.reason}."
                 if v.conflicting else f"{_quoted(v.line)} repeats {v.package.requirement()}, which Cell 2 installs."),
        details={"line": v.line.text, "cell": v.line.cell.label},
    ) for v in verdicts]
