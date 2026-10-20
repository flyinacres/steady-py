"""Creator declarations: the STEADY_PY_DECLARATIONS literal, read from a notebook cell, the live
session or a declarations file, validated, and combined into the entries the manifest records
(review_findings.md, Proposed features, install-lines spec, items 1 and 2)."""
import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from packaging.utils import canonicalize_name

from steady_py import scanning
from steady_py.constants import DECLARATIONS_NAME, DeclarationKind, DependencyStatus, Provenance, Signal
from steady_py.models import Cell, Declaration, DeclarationEvent, DiagnosticEvent, DriftFinding, ManifestPackage, Option

INSTALL, IGNORE, ACCEPT = DeclarationKind.INSTALL, DeclarationKind.IGNORE, DeclarationKind.ACCEPT
KINDS = (INSTALL, IGNORE, ACCEPT)

# The keys each kind takes besides its kind key and `reason`.
_KEYS: Dict[str, Tuple[str, ...]] = {
    INSTALL: ("version", "extras", "url", "index_url", "extra_index_url", "find_links"),
    IGNORE: ("version", "group"),
    ACCEPT: (),
}
_INDEX_OPTIONS = (("index_url", "--index-url"), ("extra_index_url", "--extra-index-url"), ("find_links", "--find-links"))
_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


class DeclarationError(ValueError):
    """Declarations that can't be used. Snapshot stops (exit 2): going on without them would pin
    every package the creator marked ignore."""


@dataclass
class DeclarationsRead:
    """The combined declarations, the warnings reading them produced, or why they can't be used."""
    entries: List[Declaration] = field(default_factory=list)
    warnings: List[DiagnosticEvent] = field(default_factory=list)
    error: Optional[str] = None


def _assignment(tree: ast.Module) -> Optional[ast.Assign]:
    """The top-level `STEADY_PY_DECLARATIONS = ...` in `tree`. Only the top level counts: an
    assignment inside a function or a branch is not the cell's declarations."""
    found = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == DECLARATIONS_NAME for t in node.targets):
            found = node
    return found


def _string(entry: Dict[str, Any], key: str, problem: List[str]) -> Optional[str]:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        problem.append(f"'{key}' must be a non-empty string")
        return None
    return value.strip()


def _strings(entry: Dict[str, Any], key: str, problem: List[str]) -> Tuple[str, ...]:
    value = entry.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        problem.append(f"'{key}' must be a string or a list of strings")
        return ()
    return tuple(v.strip() for v in value)


def _entry(value: Any, origin: str) -> Declaration:
    """One validated entry, or DeclarationError's message for it (without its location)."""
    if not isinstance(value, dict):
        raise DeclarationError(f"must be a dict such as {{\"ignore\": \"torch\", \"reason\": \"...\"}}, got {value!r}")
    kinds = [k for k in KINDS if k in value]
    if len(kinds) != 1:
        raise DeclarationError(f"needs exactly one of {', '.join(repr(k) for k in KINDS)}, got {value!r}")
    kind = kinds[0]
    unknown = sorted(str(k) for k in value if k not in (kind, "reason", *_KEYS[kind]))
    if unknown:
        allowed = ", ".join(repr(k) for k in (kind, *_KEYS[kind], "reason"))
        raise DeclarationError(f"{', '.join(repr(k) for k in unknown)} isn't a key an {kind} entry takes "
                               f"(it takes {allowed}). If this came from a newer steady-py, upgrade.")
    problem: List[str] = []
    name = _string(value, kind, problem)
    if name is not None and not _NAME.match(name):
        problem.append(f"{name!r} isn't a package{' or import' if kind == ACCEPT else ''} name")
    reason = _string(value, "reason", problem)
    if "reason" not in value:
        problem.append("needs a 'reason' saying why")
    version = _string(value, "version", problem)
    if version is not None and version[0] in "=<>!~":
        problem.append(f"'version' is an exact version such as \"2.1.0\", without an operator, got {version!r}")
    url = _string(value, "url", problem)
    if url is not None and version is not None:
        problem.append("takes 'version' or 'url', not both")
    extras = _strings(value, "extras", problem)
    group = _string(value, "group", problem)
    options: List[Option] = []
    for key, flag in _INDEX_OPTIONS:
        options += [(flag, v) for v in _strings(value, key, problem)]
    if problem or name is None or reason is None:
        raise DeclarationError("; ".join(problem) or "is incomplete")
    return Declaration(
        kind=kind, name=name if kind == ACCEPT else canonicalize_name(name), version=version,
        extras=tuple(sorted(canonicalize_name(e) for e in extras)), url=url, options=tuple(options),
        group=group, reason=reason, origin=origin,
    )


def parse(node: ast.Assign, origin: str, where: str) -> List[Declaration]:
    """The validated entries of one STEADY_PY_DECLARATIONS assignment. `where` names it in errors."""
    try:
        value = ast.literal_eval(node.value)
    except (ValueError, SyntaxError) as e:
        raise DeclarationError(f"{where}: {DECLARATIONS_NAME} isn't a plain literal (it's read, never run): {e}") from None
    if not isinstance(value, list):
        raise DeclarationError(f"{where}: {DECLARATIONS_NAME} must be a list of entries, got {type(value).__name__}")
    entries = []
    for number, item in enumerate(value, 1):
        try:
            entries.append(_entry(item, origin))
        except DeclarationError as e:
            raise DeclarationError(f"{where}: {DECLARATIONS_NAME} entry {number} {e}") from None
    return entries


def from_cells(cells: Sequence[Cell], live: bool = False) -> Tuple[List[Declaration], List[DiagnosticEvent]]:
    """The declarations a notebook's cells hold. The last cell that defines them counts: in a live
    session that is the last executed version; in a file, more than one such cell gets a warning."""
    found: List[Tuple[Cell, ast.Assign]] = []
    unparsed: Optional[Cell] = None
    warnings: List[DiagnosticEvent] = []
    for cell in cells:
        parsed = scanning.parse_cell(cell)
        if parsed.kind != "PYTHON":
            continue
        if parsed.tree is None:
            if DECLARATIONS_NAME in cell.source:
                unparsed = cell
            continue
        node = _assignment(parsed.tree)
        if node is None:
            continue
        found.append((cell, node))
        if any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "STEADY_PY_MANIFEST" for t in n.targets)
               for n in parsed.tree.body):
            warnings.append(DiagnosticEvent.at(cell, None, "declarations_with_manifest", (
                f"{cell.label} holds both {DECLARATIONS_NAME} and the manifest. Snapshot replaces the manifest's cell, "
                f"so move the declarations to a cell of their own.")))
    if not found:
        if unparsed is not None:
            raise DeclarationError(f"{unparsed.label} mentions {DECLARATIONS_NAME} but couldn't be read as Python.")
        return [], warnings
    if len(found) > 1 and not live:
        labels = ", ".join(c.label for c, _ in found)
        warnings.append(DiagnosticEvent.at(found[-1][0], None, "declarations_cells", (
            f"More than one cell defines {DECLARATIONS_NAME} ({labels}); only the last is used. Keep them in one cell.")))
    cell, node = found[-1]
    return parse(node, "cell", cell.label), warnings


def from_file(path: str) -> List[Declaration]:
    """The declarations in a declarations file. It is recorded by its file name, never its path,
    since the manifest is shared."""
    label = Path(path).name
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise DeclarationError(f"Could not read the declarations file {path}: {e}") from None
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise DeclarationError(f"{label} couldn't be read as Python: {e}") from None
    node = _assignment(tree)
    if node is None:
        raise DeclarationError(f"{label} has no top-level {DECLARATIONS_NAME} = [...]")
    return parse(node, label, label)


def _slot(entry: Declaration) -> Tuple[str, str]:
    """What two entries must share to conflict: install and ignore exclude each other for a
    package; an accept only meets another accept for the same name."""
    if entry.kind == ACCEPT:
        return (ACCEPT, canonicalize_name(entry.name))
    return ("package", entry.name)


def combine(file_entries: Sequence[Declaration], cell_entries: Sequence[Declaration]) -> Tuple[List[Declaration], List[DiagnosticEvent]]:
    """The file's entries, then the cell's, with the last entry for each slot winning whole. Every
    conflict is a warning naming both entries' origins."""
    winners: Dict[Tuple[str, str], Declaration] = {}
    warnings: List[DiagnosticEvent] = []
    for entry in (*file_entries, *cell_entries):
        slot = _slot(entry)
        earlier = winners.pop(slot, None)
        if earlier is not None:
            warnings.append(DiagnosticEvent(type="declaration_conflict", detail=(
                f"{DECLARATIONS_NAME} has more than one entry for {entry.name}: {earlier.kind} ({earlier.origin}) "
                f"and {entry.kind} ({entry.origin}). The last one, {entry.kind} ({entry.origin}), is used.")))
        winners[slot] = entry
    return list(winners.values()), warnings


def read(cells: Sequence[Cell], file_entries: Sequence[Declaration] = (), live: bool = False) -> DeclarationsRead:
    """A notebook's declarations: its cell (or live session), combined with a declarations file's."""
    try:
        cell_entries, warnings = from_cells(cells, live=live)
    except DeclarationError as e:
        return DeclarationsRead(error=str(e))
    entries, conflicts = combine(file_entries, cell_entries)
    return DeclarationsRead(entries=entries, warnings=warnings + conflicts)


# --- applying declarations ---------------------------------------------------------------------
# Declarations change outcomes only through the functions below, and each change is recorded as a
# DeclarationEvent, so no rule acts silently (item 4.5).

# Findings an accept can't cover: the accept would sit inside the manifest at fault, and the fix
# (run snapshot again, upgrade steady-py) is always available; a check error is exit 2 (decision 12).
UNACCEPTABLE_SIGNALS = frozenset({Signal.TAMPERED, Signal.UNREAD_FIELDS, Signal.CHECK_ERROR})


def _event(entry: Declaration, effect: str) -> DeclarationEvent:
    return DeclarationEvent(kind=entry.kind, name=entry.name, origin=entry.origin, effect=effect)


def _install_package(entry: Declaration, version: Optional[str]) -> ManifestPackage:
    provenance = {"version": Provenance.DECLARATION} if version else {}
    if entry.url:
        provenance["url"] = Provenance.DECLARATION
    if entry.options:
        provenance["options"] = Provenance.DECLARATION
    return ManifestPackage(
        name=entry.name, display_name=entry.name,
        status=DependencyStatus.DIRECT_REFERENCE if entry.url else DependencyStatus.PINNED,
        version=None if entry.url else version, extras=entry.extras, url=entry.url, options=entry.options,
        provenance=provenance,
    )


def apply_to_packages(
    packages: Sequence[ManifestPackage], entries: Sequence[Declaration], version_of: Callable[[str], Optional[str]],
) -> Tuple[List[ManifestPackage], List[DeclarationEvent], List[DiagnosticEvent]]:
    """The manifest's packages with install and ignore declarations applied: an ignored package is
    never pinned, and an install entry replaces whatever was inferred for its package. An install
    entry without a version or URL pins `version_of(name)` (the installed version, else PyPI's
    latest); with none found it is installed unpinned, with a warning."""
    out = list(packages)
    events: List[DeclarationEvent] = []
    warnings: List[DiagnosticEvent] = []
    for entry in entries:
        if entry.kind == ACCEPT:
            continue
        inferred = [p for p in out if p.name == entry.name]
        out = [p for p in out if p.name != entry.name]
        was = ", ".join(p.requirement() for p in inferred)
        if entry.kind == IGNORE:
            if inferred:
                events.append(_event(entry, f"not pinned (steady-py would have pinned {was})"))
            continue
        version = entry.version
        if version is None and entry.url is None:
            version = next((p.version for p in inferred if p.version), None) or version_of(entry.name)
            if version is None:
                warnings.append(DiagnosticEvent(type="declaration_unpinned", detail=(
                    f"The install declaration for {entry.name} gives no version, and steady-py found none installed or "
                    f"on PyPI, so Cell 2 can't pin it and won't install it. Add a 'version'.")))
        package = _install_package(entry, version)
        out.append(package)
        if not package.installable:
            events.append(_event(entry, "recorded, but Cell 2 can't install it without a version"))
            continue
        replaced = f", in place of {was}" if inferred and was != package.requirement() else ""
        events.append(_event(entry, f"Cell 2 installs {package.requirement()}{replaced}"))
    return out, events, warnings


def _accepts(entries: Sequence[Declaration]) -> Dict[str, Declaration]:
    return {canonicalize_name(e.name): e for e in entries if e.kind == ACCEPT}


def accept_diagnostics(diagnostics: Sequence[DiagnosticEvent], entries: Sequence[Declaration], kinds: Sequence[str]) -> Tuple[List[DeclarationEvent], Set[str]]:
    """Marks each diagnostic of `kinds` that an accept names as accepted. Returns an event per
    accepted diagnostic and the accepts that matched."""
    accepts = _accepts(entries)
    events: List[DeclarationEvent] = []
    used: Set[str] = set()
    for diagnostic in diagnostics:
        if diagnostic.type not in kinds:
            continue
        match = next((accepts[n] for n in map(canonicalize_name, diagnostic.names) if n in accepts), None)
        if match is not None:
            diagnostic.accepted = match.reason
            used.add(canonicalize_name(match.name))
            events.append(_event(match, f"{diagnostic.type} no longer counts toward exit 1"))
    return events, used


def accept_findings(findings: Sequence[DriftFinding], entries: Sequence[Declaration], counts: bool = True) -> Tuple[List[DeclarationEvent], Set[str]]:
    """Marks each finding an accept names as accepted, except those no accept can cover. With
    `counts` (check), each accepted finding is an event; at snapshot, findings don't set the exit
    code, so they are marked without one."""
    accepts = _accepts(entries)
    events: List[DeclarationEvent] = []
    used: Set[str] = set()
    for finding in findings:
        match = accepts.get(canonicalize_name(finding.package)) if finding.package else None
        if match is None or finding.signal in UNACCEPTABLE_SIGNALS:
            continue
        finding.accepted = match.reason
        used.add(canonicalize_name(match.name))
        if counts:
            events.append(_event(match, f"{finding.signal} no longer counts toward exit 1"))
    return events, used


def unused_accepts(entries: Sequence[Declaration], used: Set[str]) -> List[DeclarationEvent]:
    """An event for each accept that matched nothing in this run."""
    return [_event(e, "matched nothing in this run, so it may no longer be needed") for e in entries
            if e.kind == ACCEPT and canonicalize_name(e.name) not in used]
