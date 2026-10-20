"""Creator declarations: the STEADY_PY_DECLARATIONS literal, read from a notebook cell, the live
session or a declarations file, validated, and combined into the entries the manifest records
(review_findings.md, Proposed features, install-lines spec, items 1 and 2)."""
import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from packaging.utils import canonicalize_name

from steady_py import scanning
from steady_py.constants import DECLARATIONS_NAME, DeclarationKind
from steady_py.models import Cell, Declaration, DiagnosticEvent, Option

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
