"""Install-line reconciliation: which of a notebook's install lines can undo what Cell 2 installed
(conflicting) and which repeat it (redundant). One pure function classifies, shared by snapshot and
check (review_findings.md, install-lines spec, items 5 and 10)."""
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion

from steady_py.constants import (
    BUILD_AND_PACKAGING_TOOLS, DECLARATIONS_NAME, DeclarationKind, DependencyStatus, Readability, Severity, Signal, TargetKind, Tool,
)
from steady_py import declarations
from steady_py.models import Declaration, DiagnosticEvent, DriftFinding, InstallLine, InstallTarget, ManifestPackage

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
    if package.status == DependencyStatus.DIRECT_REFERENCE:
        return declarations.entry_text(DeclarationKind.INSTALL, package.name, url=package.url)
    return declarations.entry_text(DeclarationKind.INSTALL, package.name, version=package.version)


def _group_name(line: InstallLine, lines: Sequence[InstallLine]) -> Optional[str]:
    """The `group` that ties a guarded line's ignore entry to its alternatives: None unless another
    branch of the same guard installs something too. Named from the guard's condition, else its place."""
    guard = line.guard
    if guard is None:
        return None
    siblings = [l.guard for l in lines if l.guard is not None and l.guard.group == guard.group]
    if len({g.branch for g in siblings if g is not None}) < 2:
        return None
    condition = next((g.condition for g in sorted(siblings, key=lambda g: g.branch) if g is not None and g.condition), "")
    slug = re.sub(r"[^A-Za-z0-9_]+", "-", condition).strip("-")[:40]
    return slug or "cell-" + guard.group.replace(":", "-line-")


def _ignore_entry(name: str, line: InstallLine, lines: Sequence[InstallLine]) -> str:
    return declarations.entry_text(DeclarationKind.IGNORE, name, group=_group_name(line, lines))


def _advice(verdict: LineVerdict, imported: Set[str], companion: bool, lines: Sequence[InstallLine],
            declared: Set[str]) -> Tuple[str, Tuple[str, ...]]:
    """What to do with a conflicting line (item 5.2), and the entries it offers: delete it when an
    import or an install declaration keeps the pin alive, else replace it with an install declaration.
    A guarded line runs only sometimes, so it is never just deleted."""
    package = verdict.package
    if verdict.line.guard is not None:
        ignore = _ignore_entry(package.name, verdict.line, lines)
        return (f"It runs only when its condition holds. If the notebook needs that, add {ignore} to "
                f"{DECLARATIONS_NAME} so Cell 2 leaves it alone; otherwise delete the line."), (ignore,)
    shared = len(verdict.line.targets) > 1  # the line installs other packages too: only this one goes
    remove = f"Remove {package.name} from the line" if shared else "Delete the line"
    if package.name in imported or package.name in declared:
        keeper = "the import keeps the pin" if package.name in imported else "your install declaration keeps the pin"
        note = " Once it's deleted, the source notebook installs nothing without the companion." if companion and not shared else ""
        return f"{remove}; {keeper}.{note}", ()
    install = _install_entry(package)
    return f"{remove} and add {install} to {DECLARATIONS_NAME}, so the pin doesn't depend on this line.", (install,)


def _redundant(verdict: LineVerdict) -> str:
    return f"{_quoted(verdict.line)} repeats {verdict.package.requirement()}, which Cell 2 installs, and changes nothing. No change needed."


def snapshot_diagnostics(verdicts: Sequence[LineVerdict], imported: Set[str], companion: bool,
                         lines: Sequence[InstallLine] = (), entries: Sequence[Declaration] = ()) -> List[DiagnosticEvent]:
    """Snapshot's advice, one diagnostic per line and package: a conflicting line is a warning that
    sets exit 1 unless accepted (item 10.1; the accept entry is offered with every attention
    diagnostic, declarations.accept_diagnostics). A redundant line is information only, with no
    advice: keeping it is harmless, and the next snapshot still harvests its options (item 5.2)."""
    declared = {e.name for e in entries if e.kind == DeclarationKind.INSTALL}
    out = []
    for v in verdicts:
        package = v.package
        if v.conflicting:
            advice, offered = _advice(v, imported, companion, lines, declared)
            event = DiagnosticEvent.at(v.line.cell, v.line.line_idx, "conflicting_install_line", (
                f"{_quoted(v.line)} can change {package.label} after Cell 2 installs {package.requirement()}: it {v.reason}. "
                f"{advice}"))
            event.entries = offered
        else:
            event = DiagnosticEvent.at(v.line.cell, v.line.line_idx, "redundant_install_line",
                                       _redundant(v), level="notice")
        event.names = (package.name,)
        out.append(event)
    return out


def guards(lines: Sequence[InstallLine]) -> Dict[str, Dict[str, Any]]:
    """The install-line guard of each package that only guarded lines install (fix_plan.md, section
    3.2.2): its kind, group and branch, from the first such line, for `packages.guard`."""
    found: Dict[str, Dict[str, Any]] = {}
    unguarded: Set[str] = set()
    for line in lines:
        if not line.kernel_target or line.readability != Readability.LITERAL or line.tool == Tool.SYSTEM:
            continue
        for target in line.targets:
            name = _named(target)
            if name is None:
                continue
            if line.guard is None:
                unguarded.add(name)
            else:
                found.setdefault(name, {"kind": line.guard.kind, "group": line.guard.group, "branch": line.guard.branch})
    return {name: guard for name, guard in found.items() if name not in unguarded}


def _proposal(line: InstallLine, name: str, detail: str, entries: Tuple[str, ...]) -> DiagnosticEvent:
    event = DiagnosticEvent.at(line.cell, line.line_idx, "declaration_proposal", detail, level="notice")
    event.names = (name,) if name else ()
    event.entries = entries
    return event


def _names(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _listed(entries: Sequence[str]) -> str:
    return entries[0] if len(entries) == 1 else ", ".join(entries)


# pip and the build tools an install line upgrades are never pinned (BUILD_AND_PACKAGING_TOOLS).
_TOOLS = frozenset(canonicalize_name(t) for t in BUILD_AND_PACKAGING_TOOLS)


def _exact_version(specifier: str) -> Optional[str]:
    match = re.fullmatch(r"\s*===?\s*([^\s,*]+)\s*", specifier or "")
    return match.group(1) if match else None


def proposals(lines: Sequence[InstallLine], packages: Sequence[ManifestPackage],
              entries: Sequence[Declaration]) -> List[DiagnosticEvent]:
    """Declarations snapshot proposes for install lines Cell 2 doesn't reproduce and the creator hasn't
    declared (items 3 and 5.4): guarded alternatives as ignore entries sharing a group, conda lines as
    ignore entries, computed lines and requirement files as entries to fill in, packages that weren't
    installed as install entries, and an extra a line asks for that the pin lacks. Each is a notice
    with its entries ready to paste."""
    declared = {e.name for e in entries if e.kind in (DeclarationKind.INSTALL, DeclarationKind.IGNORE)}
    installs = {p.name: p for p in packages if p.installable}
    out: List[DiagnosticEvent] = []
    for line in lines:
        if not line.kernel_target or line.tool == Tool.SYSTEM or line.readability == Readability.UNREADABLE:
            continue
        where = _quoted(line)
        if line.readability == Readability.COMPUTED:
            offered: Tuple[str, ...] = (declarations.entry_text(DeclarationKind.INSTALL, "<package>"), declarations.entry_text(DeclarationKind.IGNORE, "<package>"))
            out.append(_proposal(line, "", (
                f"{where} names its packages with a variable, so steady-py can't see them. For each package it "
                f"installs, add {offered[0]} to {DECLARATIONS_NAME} to have Cell 2 pin it, or {offered[1]} to leave "
                f"it to this line."), offered))
            continue
        guarded: List[str] = []
        conda: List[str] = []
        missing: List[Tuple[str, Optional[str]]] = []
        for target in line.targets:
            if target.kind == TargetKind.REQUIREMENTS_FILE:
                offered = (declarations.entry_text(DeclarationKind.INSTALL, "<package>"),
                           declarations.entry_text(DeclarationKind.IGNORE, "<package>"))
                out.append(_proposal(line, "", (
                    f"{where} installs from {target.text}, which steady-py doesn't read. For each package in it the "
                    f"notebook needs, add {offered[0]} to {DECLARATIONS_NAME} to have Cell 2 pin it, or {offered[1]} "
                    f"to leave it to this line."), offered))
                continue
            name = _named(target)
            if name is None or name in declared or name in _TOOLS or name in (*guarded, *conda, *(n for n, _ in missing)):
                continue
            package = installs.get(name)
            if package is not None:
                wanted = {canonicalize_name(e) for e in target.extras}
                lacking = wanted - {canonicalize_name(e) for e in package.extras}
                if lacking and package.status == DependencyStatus.PINNED:
                    extras = sorted(wanted | {canonicalize_name(e) for e in package.extras})
                    entry = declarations.entry_text(DeclarationKind.INSTALL, name, version=package.version, extras=extras)
                    out.append(_proposal(line, name, (
                        f"{where} asks for {name}[{','.join(sorted(lacking))}], but Cell 2 installs {package.requirement()} "
                        f"without that extra. To require it, add {entry} to {DECLARATIONS_NAME}."), (entry,)))
                continue  # otherwise the line's verdict says what to do with it
            if line.guard is not None:
                guarded.append(name)
            elif target.kind == TargetKind.NAME:
                conda.append(name)
            elif target.kind == TargetKind.REQUIREMENT:
                missing.append((name, _exact_version(target.specifier)))
        if guarded:
            offered = tuple(_ignore_entry(n, line, lines) for n in guarded)
            alternatives = " with its alternatives" if '"group"' in offered[0] else ""
            out.append(_proposal(line, guarded[0], (
                f"{where} runs only when its condition holds, so Cell 2 leaves {_names(guarded)} to it. To confirm, add "
                f"{_listed(offered)} to {DECLARATIONS_NAME}; Cell 2 then reports what is installed{alternatives}."), offered))
        if conda:
            offered = tuple(declarations.entry_text(DeclarationKind.IGNORE, n) for n in conda)
            out.append(_proposal(line, conda[0], (
                f"{where} is a conda install, which Cell 2 can't reproduce. To confirm the line handles "
                f"{_names(conda)}, add {_listed(offered)} to {DECLARATIONS_NAME}."), offered))
        if missing:
            installs_offered = tuple(declarations.entry_text(DeclarationKind.INSTALL, n, version=v) for n, v in missing)
            ignores = tuple(declarations.entry_text(DeclarationKind.IGNORE, n) for n, _ in missing)
            out.append(_proposal(line, missing[0][0], (
                f"{where} installs {_names([n for n, _ in missing])}, not installed here, so Cell 2 can't pin "
                f"{'it' if len(missing) == 1 else 'them'}. To have Cell 2 install {'it' if len(missing) == 1 else 'them'}, "
                f"add {_listed(installs_offered)} to {DECLARATIONS_NAME} (without a version, Cell 2 pins PyPI's latest); "
                f"to leave {'it' if len(missing) == 1 else 'them'} to this line, add {_listed(ignores)}."),
                (*installs_offered, *ignores)))
    return out


def check_findings(verdicts: Sequence[LineVerdict]) -> List[DriftFinding]:
    """Check's view (item 10.2): a conflicting line is a confirmed finding, a redundant one a notice.
    Check gives no delete-or-replace advice, since that needs the import join and so the environment."""
    return [DriftFinding(
        package=v.package.name, version=v.package.version or "",
        signal=Signal.CONFLICTING_LINE if v.conflicting else Signal.REDUNDANT_LINE,
        severity=Severity.CONFIRMED if v.conflicting else Severity.NOTICE,
        message=(f"{_quoted(v.line)} can change {v.package.label} after Cell 2 installs {v.package.requirement()}: it {v.reason}."
                 if v.conflicting else _redundant(v)),
        details={"line": v.line.text, "cell": v.line.cell.label},
    ) for v in verdicts]
