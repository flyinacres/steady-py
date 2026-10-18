"""Data types shared across layers: diagnostics, dependency entries, the manifest and its pins, the
generation-time baseline, drift findings and their identity keys, and the analysis reports."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from steady_py.constants import (
    INSTALLABLE_STATUSES, MANIFEST_SCHEMA_VERSION, PIP_OPTIONS, TOOL_VERSION, DependencyStatus, Invocation, Provenance, Readability,
    Signal,
)


@dataclass(frozen=True)
class Cell:
    """One code cell as the reader found it: raw source and where the user sees it.

    `position` is the 0-based index among all of a notebook's cells (markdown included); a live
    session has none. `execution_count` is the kernel's count; in a live session it is the history
    index. `heading` is the nearest markdown heading above the cell.
    """
    source: str
    position: Optional[int] = None
    execution_count: Optional[int] = None
    heading: Optional[str] = None

    @property
    def label(self) -> str:
        """How the user finds the cell: its 1-based notebook position, or its In [n] in a session."""
        if self.position is not None:
            return f"Cell {self.position + 1}"
        if self.execution_count is not None:
            return f"In [{self.execution_count}]"
        return "a cell"


@dataclass
class DiagnosticEvent:
    """A structured warning or notice, located by notebook and by cell as the user sees them."""
    type: str
    detail: str
    cell_idx: Optional[int] = None  # the cell's notebook position (Cell.position)
    line_idx: Optional[int] = None
    level: str = "warning"
    cell_label: Optional[str] = None
    notebook: Optional[str] = None

    @classmethod
    def at(cls, cell: Optional[Cell], line_idx: Optional[int], type: str, detail: str, level: str = "warning") -> "DiagnosticEvent":
        """A diagnostic located at `cell`, or unlocated when there is no cell."""
        if cell is None:
            return cls(type=type, detail=detail, line_idx=line_idx, level=level)
        return cls(type=type, detail=detail, cell_idx=cell.position, line_idx=line_idx, level=level, cell_label=cell.label)

    def format_console(self) -> str:
        prefix = "⚠️" if self.level == "warning" else "ℹ️"
        where = f"{self.cell_label}: " if self.cell_label else ""
        return f"{prefix} {where}{self.detail}"

    def __str__(self) -> str:
        return self.format_console()

    def __contains__(self, item: str) -> bool:
        return item in self.detail or item in self.format_console()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "detail": self.detail,
            "cell_idx": self.cell_idx,
            "line_idx": self.line_idx,
        }


@dataclass
class PromotionDetail:
    """Represents an automatic extras package promotion event."""
    import_name: str
    promoted_name: str
    version: Optional[str] = None
    detail: str = ""

    def __str__(self) -> str:
        return self.detail

    def __contains__(self, item: str) -> bool:
        return item in self.detail or item in self.promoted_name or item in self.import_name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "import": self.import_name,
            "promoted_name": self.promoted_name,
            "version": self.version,
        }


@dataclass
class PipInstallOccurrence:
    """
    Represents an explicit package install invocation inside a notebook cell.
    
    Fields:
        cell_idx: Index of the cell (document or execution order rank).
        line_idx: Line index inside the cell where the install was declared.
        raw_token: Raw CLI argument token (e.g. 'torch==2.3.1+cu121').
        name: PyPI distribution stem (e.g. 'torch').
        version_spec: Version specifier string if present (e.g. '==2.3.1+cu121', '>=2.0'), else empty.
        flags: Scoped CLI flags accompanying this specific command.
    """
    cell_idx: int
    line_idx: int
    raw_token: str
    name: str
    version_spec: str = ""
    flags: List[str] = field(default_factory=list)
    cell: Optional[Cell] = None
    extras: List[str] = field(default_factory=list)


@dataclass
class ImportOccurrence:
    """
    Represents an AST import statement inside a notebook cell.
    
    Fields:
        cell_idx: Index of the cell.
        line_idx: Zero-indexed line number in the original cell source.
        module: Base imported package stem (e.g. 'torch').
        full_name: Full imported module string (e.g. 'umap.plot').
        is_guarded: True if import occurred inside a try/except or conditional block.
        from_names: For `from x import a, b`, the names imported ('a', 'b').
    """
    cell_idx: int
    line_idx: int
    module: str
    full_name: str = ""
    is_guarded: bool = False
    cell: Optional[Cell] = None
    from_names: Tuple[str, ...] = ()  # the names a `from x import a, b` brings in

    @property
    def dotted_names(self) -> Set[str]:
        """Every dotted name this import could load: the module, and for a from-import each name
        under it, which is a submodule when the module is a namespace package (P2)."""
        base = self.full_name or self.module
        return {base, *(f"{base}.{name}" for name in self.from_names)}


@dataclass(frozen=True)
class Guard:
    """The innermost condition an install line or import runs under (fix_plan.md, section 3.1, item 6).

    `kind` is if, try, except or function (Python), or shell_joined or shell_conditional. `condition`
    is the if or elif test, the except clause's exception type, the function name, or the shell text.
    `group` identifies one if/elif/else chain or try statement; `branch` is the clause's index in it,
    so lines in one group with different branches are mutually exclusive.
    """
    kind: str
    condition: str
    group: str
    branch: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "condition": self.condition, "group": self.group, "branch": self.branch}


@dataclass
class InstallTarget:
    """One thing an install line installs (fix_plan.md, section 3.1, item 8). `text` is the token as
    written. For a requirement, `name` is as typed and `canonical` normalized, with its extras,
    specifier and marker; a `name @ url` keeps its name too."""
    kind: str
    text: str
    name: str = ""
    canonical: str = ""
    extras: List[str] = field(default_factory=list)
    specifier: str = ""
    marker: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "name": self.name, "canonical": self.canonical,
                "extras": list(self.extras), "specifier": self.specifier, "marker": self.marker}


@dataclass
class InstallOption:
    """One option on an install line: the canonical long name (`-i x` and `--index-url=x` give the
    same pair), or the option as written when the tool's table doesn't know it. `value` is None for a
    switch."""
    name: str
    value: Optional[str] = None
    known: bool = True

    def as_args(self) -> List[str]:
        return [self.name] if self.value is None else [self.name, self.value]

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "value": self.value, "known": self.known}


@dataclass
class InstallLine:
    """One package-install command as written in a notebook (fix_plan.md, section 3.1).

    `tool` is pip, uv, conda or system. `invocation` is line_magic, shell_escape, shell_cell or
    python_call. `kernel_target` is False when the line installs somewhere other than the kernel's
    environment, with the option that says so in `elsewhere`. Options apply to every target on the line.
    """
    text: str
    tool: str
    cell: Cell
    line_idx: int
    notebook: Optional[str] = None
    invocation: str = Invocation.SHELL_ESCAPE
    guard: Optional[Guard] = None
    readability: str = Readability.LITERAL
    kernel_target: bool = True
    elsewhere: str = ""
    targets: List[InstallTarget] = field(default_factory=list)
    options: List[InstallOption] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "tool": self.tool,
            "invocation": self.invocation,
            "guard": self.guard.to_dict() if self.guard else None,
            "readability": self.readability,
            "kernel_target": self.kernel_target,
            "elsewhere": self.elsewhere,
            "targets": [t.to_dict() for t in self.targets],
            "options": [o.to_dict() for o in self.options],
            "notebook": self.notebook,
            "cell_idx": self.cell.position,
            "line_idx": self.line_idx,
            "execution_count": self.cell.execution_count,
            "heading": self.cell.heading,
        }


@dataclass(frozen=True)
class RawInstall:
    """An install line target that isn't a PyPI pin (a URL, `name @ url` or local path), with the
    options from its line that change what gets installed (G14). The manifest records it as a
    direct_reference package."""
    spec: str
    flags: Tuple[str, ...] = ()


@dataclass
class DependencyEntry:
    """
    Represents a single dependency requirement with scoped flags or an informational comment.

    Fields:
        name: Distribution / PyPI package name (e.g., 'torch', 'pandas').
        version: Pinned version string (e.g., '2.3.1+cu121', '2.2.1') or empty if unversioned.
        flags: Scoped CLI flags to pass to pip install (e.g., ['--extra-index-url', 'https://...']).
        source: Discovery origin ('import', 'pip_command', 'writefile_script').
        status: Semantic category ('pinned', 'guarded', 'platform_pseudo_module', 'build_tool', 'local_module', 'auxiliary_tool', 'writefile_script',
            'direct_reference', 'system_path').
        is_comment: True if this entry represents a comment, platform pseudo-module, or uninstalled fallback.
        comment_text: Full string representation when is_comment is True.
        anchor: Which directory a 'local_module' status entry resolved against
            ('notebook_dir' or 'root_dir'); empty for every other status. Never
            carries a path -- only the anchor name -- so a shared notebook never
            bakes in the creator's directory structure (see LocalModule).
        direct_url: For a 'direct_reference' entry (installed from a remote git/archive URL,
            not PyPI): the source spec to carry into the manifest's raw_installs. Always empty
            for a 'system_path' entry -- a local path is never stored anywhere.
    """
    name: str = ""
    version: str = ""
    flags: List[str] = field(default_factory=list)
    source: str = "import"
    status: str = DependencyStatus.PINNED
    is_comment: bool = False
    comment_text: str = ""
    anchor: str = ""
    direct_url: str = ""
    version_source: str = Provenance.ENVIRONMENT  # Provenance.NOTEBOOK when the install line's version won

    @property
    def specifier(self) -> str:
        """Returns the pip install specifier (e.g. 'pandas==2.2.1')."""
        if self.is_comment:
            return self.comment_text
        return f"{self.name}=={self.version}" if self.version else self.name

    def to_dict(self) -> Dict[str, Any]:
        """Converts to a dictionary representation for Cell 2 inline execution metadata."""
        return {
            "name": self.name,
            "version": self.version,
            "flags": self.flags
        }

    def to_report_dict(self) -> Dict[str, Any]:
        """Converts to full JSON report representation."""
        return {
            "name": self.name,
            "version": self.version if self.version else None,
            "source": self.source,
            "status": self.status,
            "hardware_tagged": ("+" in self.version) if self.version else False,
            "flags": self.flags,
            "comment": self.comment_text if self.is_comment else None
        }


@dataclass
class TimelineResult:
    """
    Encapsulates the resolved dependency timeline and associated promotion and conflict notices.

    Fields:
        dependencies: Ordered list of DependencyEntry objects.
        promotion_notices: Informational PromotionDetail objects.
        conflict_warnings: Structured diagnostic events for overwritten pins or conflicting flags.
        warnings: What the creator must act on for the imports themselves (an editable install,
            a package installed from a local file, a custom path, an ambiguous import).
        notices: Informational events about the imports (a conda-built package).
    """
    dependencies: List[DependencyEntry] = field(default_factory=list)
    promotion_notices: List[PromotionDetail] = field(default_factory=list)
    conflict_warnings: List[DiagnosticEvent] = field(default_factory=list)
    warnings: List[DiagnosticEvent] = field(default_factory=list)
    notices: List[DiagnosticEvent] = field(default_factory=list)

    def add(self, resolved: Any) -> None:
        """Appends one import's or install line's entries and diagnostics (a resolution.Resolved)."""
        self.dependencies.extend(resolved.entries)
        if resolved.promotion and resolved.promotion not in self.promotion_notices:
            self.promotion_notices.append(resolved.promotion)
        self.warnings.extend(resolved.warnings)
        self.notices.extend(resolved.notices)

    def __iter__(self) -> Iterator[Any]:
        """Unpacking fallback allowing `deps, notices = timeline_result` in legacy callers."""
        notice_strings = [p.detail for p in self.promotion_notices]
        return iter((self.dependencies, notice_strings))


@dataclass
class GpuInfo:
    """Payload representing active host accelerator capabilities across PyTorch, TensorFlow, and JAX."""
    has_gpu: bool = False
    type: Optional[str] = None
    active_framework: Optional[str] = None
    device_name: Optional[str] = None
    frameworks: List[str] = field(default_factory=list)
    framework_devices: Dict[str, Optional[str]] = field(default_factory=dict)
    probe_errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_gpu": self.has_gpu,
            "framework": self.active_framework,
            "device_name": self.device_name,
            "frameworks_detected": self.frameworks,
            "probe_errors": self.probe_errors
        }


BASELINE_FORMAT_VERSION = 1
FindingKey = Tuple[str, ...]  # see finding_baseline_key


@dataclass(frozen=True)
class Baseline:
    """What generation-time validation found: compact finding keys, plus the packages that could not
    be checked ("" means the transitive resolution itself failed). Persisted in the manifest as a
    dict (to_dict / from_dict)."""
    findings: Tuple[FindingKey, ...] = ()
    errors: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": BASELINE_FORMAT_VERSION,
            "findings": [list(key) for key in self.findings],
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["Baseline"]:
        """None when `data` is absent or not a baseline format this tool understands, so an
        unrecognized baseline reads as 'no baseline recorded' rather than as an error."""
        if not isinstance(data, dict) or data.get("version") != BASELINE_FORMAT_VERSION:
            return None
        findings, errors = data.get("findings"), data.get("errors")
        if not isinstance(findings, list) or not isinstance(errors, list):
            return None
        if not all(isinstance(k, (list, tuple)) and all(isinstance(part, str) for part in k) for k in findings):
            return None
        if not all(isinstance(e, str) for e in errors):
            return None
        return cls(findings=tuple(tuple(k) for k in findings), errors=tuple(errors))


@dataclass(frozen=True)
class PinnedDependency:
    """One pin as the drift checks consume it: a manifest package Cell 2 installs from an index.

    `name` may carry an extras tag ("pandas[test]"); `flags` are the pip arguments of its options.
    Built from the manifest (SteadyPyManifest.pins), never persisted.
    """
    name: str
    version: str
    flags: Tuple[str, ...] = ()


class ManifestError(ValueError):
    """A manifest this steady-py can't read. The message says why and what to do. `newer` is True
    when a newer steady-py wrote it (a format this one doesn't know), `older` when it predates the
    format this one reads."""

    def __init__(self, message: str, newer: bool = False, older: bool = False):
        super().__init__(message)
        self.newer = newer
        self.older = older


# One install option: its canonical name and value (None for a switch), as the install-line record
# stores options (fix_plan.md, section 3.1.9).
Option = Tuple[str, Optional[str]]


def option_pairs(args: Sequence[str]) -> Tuple[Option, ...]:
    """Canonical (name, value) pairs from the pip arguments a carried option list holds. The names are
    already canonical (InstallOption.as_args), so the option table only says which take a value."""
    pairs: List[Option] = []
    i = 0
    while i < len(args):
        name, takes_value = PIP_OPTIONS.get(args[i], (args[i], False))
        value = args[i + 1] if takes_value and i + 1 < len(args) else None
        pairs.append((name, value))
        i += 2 if takes_value else 1
    return tuple(pairs)


def option_args(options: Sequence[Option]) -> List[str]:
    """The pip arguments for a list of options."""
    return [arg for name, value in options for arg in ((name,) if value is None else (name, value))]


def canonical_extras(extras: Sequence[str]) -> Tuple[str, ...]:
    """Extras as they compare (PEP 685): normalized, sorted, without duplicates."""
    return tuple(sorted({canonicalize_name(e) for e in extras}))


def _str_or_none(value: Any, what: str) -> Optional[str]:
    if value is not None and not isinstance(value, str):
        raise ManifestError(f"the manifest's {what} must be a string, got {value!r}")
    return value


def _str_list(value: Any, what: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"the manifest's {what} must be a list of strings, got {value!r}")
    return tuple(value)


def _options(value: Any, what: str) -> Tuple[Option, ...]:
    if value is None:
        return ()
    ok = isinstance(value, list) and all(
        isinstance(p, (list, tuple)) and len(p) == 2 and isinstance(p[0], str) and (p[1] is None or isinstance(p[1], str))
        for p in value)
    if not ok:
        raise ManifestError(f"the manifest's {what} must be a list of [name, value] pairs, got {value!r}")
    return tuple((p[0], p[1]) for p in value)


def _provenance(value: Any, what: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ManifestError(f"the manifest's {what} must map field names to sources, got {value!r}")
    return dict(value)


def _entry_dict(value: Any, what: str, required: Tuple[str, ...], known: Set[str], unknown: Set[str]) -> Dict[str, Any]:
    """A manifest entry dict with its required string fields present; its unknown keys are added to
    `unknown` as `<what>.<key>`."""
    if not isinstance(value, dict):
        raise ManifestError(f"each of the manifest's {what} must be a dict, got {value!r}")
    for key in required:
        if not isinstance(value.get(key), str):
            raise ManifestError(f"each of the manifest's {what} needs a string '{key}', got {value!r}")
    unknown.update(f"{what}.{k}" for k in value if k not in known)
    return value


@dataclass
class ManifestPackage:
    """One package the notebook needs, as the manifest records it.

    `name` is the normalized distribution name, or the import name for a module no installed
    distribution owns (custom_path, ambiguous); "" for a direct source whose name isn't known.
    `display_name` is the name as installed (the metadata Name), for messages; it is carried forward
    while `name` is unchanged. `status` is a DependencyStatus value: Cell 2 installs only
    INSTALLABLE_STATUSES, and the rest are records for check and the creator (DG4). `extras` are as
    written; they compare normalized. `provenance` maps each sourced field (version, url, options) to
    a Provenance value. `guard` is reserved for the install-line guard (kind, group, branch), filled
    once install lines are reconciled (fix_plan.md, step 4). `changed_at` is when this entry's content
    last changed.
    """
    name: str
    status: str
    display_name: str = ""
    version: Optional[str] = None
    extras: Tuple[str, ...] = ()
    url: Optional[str] = None
    options: Tuple[Option, ...] = ()
    custom_sourced: bool = False
    guard: Optional[Dict[str, Any]] = None
    provenance: Dict[str, str] = field(default_factory=dict)
    changed_at: str = ""

    _FIELDS = ("name", "display_name", "status", "version", "extras", "url", "options", "custom_sourced",
               "guard", "provenance", "changed_at")

    @property
    def key(self) -> Tuple[str, Tuple[str, ...]]:
        """What matches this entry across manifests: the normalized name (the URL for a nameless
        direct source) and the normalized extras."""
        return (self.name or (self.url or ""), canonical_extras(self.extras))

    @property
    def label(self) -> str:
        """The entry as messages name it: display name and extras, or its URL."""
        base = self.display_name or self.name or (self.url or "")
        return f"{base}[{','.join(self.extras)}]" if self.extras else base

    @property
    def installable(self) -> bool:
        return self.status in INSTALLABLE_STATUSES and bool(self.version or self.url)

    def requirement(self) -> str:
        """What Cell 2 passes pip: `name[extras]==version`, or the direct source as written."""
        if self.status == DependencyStatus.DIRECT_REFERENCE and self.url:
            return self.url
        return f"{self.label}=={self.version}" if self.version else self.label

    def content(self) -> Dict[str, Any]:
        """The fields that make a change: everything but the display name and the change date."""
        out = self.to_dict()
        del out["display_name"], out["changed_at"]
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "display_name": self.display_name, "status": self.status, "version": self.version,
            "extras": list(self.extras), "url": self.url, "options": [list(o) for o in self.options],
            "custom_sourced": self.custom_sourced, "guard": self.guard, "provenance": dict(self.provenance),
            "changed_at": self.changed_at,
        }

    @classmethod
    def from_dict(cls, data: Any, unknown: Set[str]) -> "ManifestPackage":
        d = _entry_dict(data, "packages", ("name", "status"), set(cls._FIELDS), unknown)
        guard = d.get("guard")
        if guard is not None and not isinstance(guard, dict):
            raise ManifestError(f"a package's 'guard' must be a dict, got {guard!r}")
        return cls(  # the name is the key, so it is read normalized whatever spelling was stored (CH2)
            name=canonicalize_name(d["name"]) if d["name"] else "", status=d["status"],
            display_name=_str_or_none(d.get("display_name"), "display_name") or d["name"],
            version=_str_or_none(d.get("version"), "version"), extras=_str_list(d.get("extras"), "extras"),
            url=_str_or_none(d.get("url"), "url"), options=_options(d.get("options"), "options"),
            custom_sourced=bool(d.get("custom_sourced")), guard=guard,
            provenance=_provenance(d.get("provenance"), "provenance"), changed_at=_str_or_none(d.get("changed_at"), "changed_at") or "",
        )

    def to_pin(self) -> PinnedDependency:
        return PinnedDependency(name=self.label, version=self.version or "", flags=tuple(option_args(self.options)))


@dataclass
class LocalModule:
    """A local module found at generation time, for check to re-verify by existence (never a PyPI
    check: these were never installable). `anchor` is "notebook_dir" or "root_dir", never a path, so
    a shared notebook never bakes in the creator's directory structure."""
    name: str
    anchor: str
    provenance: Dict[str, str] = field(default_factory=dict)
    changed_at: str = ""

    _FIELDS = ("name", "anchor", "provenance", "changed_at")

    @property
    def key(self) -> Tuple[str, Tuple[str, ...]]:
        return (self.name, ())

    @property
    def label(self) -> str:
        return self.name

    def content(self) -> Dict[str, Any]:
        out = self.to_dict()
        del out["changed_at"]
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "anchor": self.anchor, "provenance": dict(self.provenance), "changed_at": self.changed_at}

    @classmethod
    def from_dict(cls, data: Any, unknown: Set[str]) -> "LocalModule":
        d = _entry_dict(data, "local_modules", ("name", "anchor"), set(cls._FIELDS), unknown)
        return cls(name=d["name"], anchor=d["anchor"], provenance=_provenance(d.get("provenance"), "provenance"),
                   changed_at=_str_or_none(d.get("changed_at"), "changed_at") or "")


@dataclass
class Declaration:
    """A creator declaration as recorded (review_findings.md, Proposed features): `kind` is "install"
    or "ignore"; `origin` is "cell" or the declarations file's label. Recorded from step 4 on."""
    kind: str
    name: str
    version: Optional[str] = None
    options: Tuple[Option, ...] = ()
    group: Optional[str] = None
    reason: str = ""
    origin: str = ""
    changed_at: str = ""

    _FIELDS = ("kind", "name", "version", "options", "group", "reason", "origin", "changed_at")

    @property
    def key(self) -> Tuple[str, Tuple[str, ...]]:
        return (canonicalize_name(self.name), ())

    @property
    def label(self) -> str:
        return self.name

    def content(self) -> Dict[str, Any]:
        out = self.to_dict()
        del out["changed_at"]
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "version": self.version, "options": [list(o) for o in self.options],
                "group": self.group, "reason": self.reason, "origin": self.origin, "changed_at": self.changed_at}

    @classmethod
    def from_dict(cls, data: Any, unknown: Set[str]) -> "Declaration":
        d = _entry_dict(data, "declarations", ("kind", "name"), set(cls._FIELDS), unknown)
        return cls(kind=d["kind"], name=d["name"], version=_str_or_none(d.get("version"), "version"),
                   options=_options(d.get("options"), "options"), group=_str_or_none(d.get("group"), "group"),
                   reason=_str_or_none(d.get("reason"), "reason") or "", origin=_str_or_none(d.get("origin"), "origin") or "",
                   changed_at=_str_or_none(d.get("changed_at"), "changed_at") or "")


def _manifest_payload_hash(payload: Dict[str, Any]) -> str:
    """SHA-256 of canonical (sorted-key) JSON of every field except dependency_hash itself."""
    body = {k: v for k, v in payload.items() if k != "dependency_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _format_version(value: Any) -> Tuple[int, int]:
    """(major, minor) of a "major.minor" schema version; ManifestError when it isn't one."""
    if isinstance(value, str):
        parts = value.split(".")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            return int(parts[0]), int(parts[1])
    raise ManifestError(f"the manifest's schema_version {value!r} is not a format version steady-py knows")


_MANIFEST_FIELDS = ("schema_version", "tool_version", "generated_at", "dependency_hash", "python_version", "gpu",
                    "baseline", "packages", "local_modules", "declarations")


@dataclass
class SteadyPyManifest:
    """The reproducibility manifest embedded in generated Cell 2 as STEADY_PY_MANIFEST, and what check
    parses back out of a notebook or .py file (fix_plan.md, section 3.2).

    It records state with its history: each entry's provenance and last change date, and
    `generated_at`, when the manifest's content last changed. `baseline` records what
    generation-time validation found, as compact keys (see build_baseline), so a later check can tell
    findings that were already present from new ones; None means none was recorded.
    """
    python_version: Dict[str, int]
    packages: List[ManifestPackage] = field(default_factory=list)
    gpu: Optional[Dict[str, Any]] = None
    generated_at: str = ""
    tool_version: str = TOOL_VERSION
    schema_version: str = MANIFEST_SCHEMA_VERSION
    dependency_hash: str = ""
    local_modules: List[LocalModule] = field(default_factory=list)
    declarations: List[Declaration] = field(default_factory=list)
    baseline: Optional[Baseline] = None
    # Set only by from_literal, and never serialized, hashed or compared: the hash recomputed over the
    # fields exactly as they were persisted, and the fields this steady-py doesn't know.
    verified_hash: Optional[str] = field(default=None, init=False, repr=False, compare=False)
    unknown_fields: Tuple[str, ...] = field(default=(), init=False, repr=False, compare=False)

    def pins(self) -> List[PinnedDependency]:
        """The packages Cell 2 installs from an index, as the drift checks consume them."""
        return [p.to_pin() for p in self.packages if p.installable and p.status == DependencyStatus.PINNED]

    def installs(self) -> List[ManifestPackage]:
        """What Cell 2 installs, index pins first and then direct sources, each in manifest order."""
        pinned = [p for p in self.packages if p.installable and p.status == DependencyStatus.PINNED]
        return pinned + [p for p in self.packages if p.installable and p.status != DependencyStatus.PINNED]

    @classmethod
    def from_literal(cls, data: Any) -> "SteadyPyManifest":
        """Reads a persisted STEADY_PY_MANIFEST literal, or raises ManifestError saying why it can't.

        Same major format version: fields this steady-py doesn't know are ignored and listed in
        unknown_fields, and a missing optional field reads as not recorded. A missing python_version
        or packages, or a different major version, can't be read. The integrity hash is recomputed
        over `data` as found, so an added field never makes an older manifest look hand-edited, and a
        deleted one is still caught.
        """
        if not isinstance(data, dict):
            raise ManifestError(f"the manifest literal must be a dict, not {type(data).__name__}")
        recorded_tool = data.get("tool_version")
        tool_version = recorded_tool if isinstance(recorded_tool, str) else ""
        writer = tool_version or "an unknown version"
        major, _minor = _format_version(data.get("schema_version"))
        ours = _format_version(MANIFEST_SCHEMA_VERSION)[0]
        if major > ours:
            raise ManifestError(
                f"it was written by steady-py {writer} in a newer format ({data['schema_version']}); "
                f"upgrade steady-py to read it", newer=True)
        if major < ours:
            raise ManifestError(
                f"it was written by steady-py {writer} in an older format ({data['schema_version']}); "
                f"run snapshot again to replace it", older=True)
        python_version, packages = data.get("python_version"), data.get("packages")
        if not isinstance(python_version, dict) or not all(isinstance(v, int) for v in python_version.values()):
            raise ManifestError("the manifest has no readable 'python_version'")
        if not isinstance(packages, list):
            raise ManifestError("the manifest has no readable 'packages' list")
        gpu = data.get("gpu")
        if gpu is not None and not isinstance(gpu, dict):
            raise ManifestError(f"the manifest's 'gpu' must be a dict or None, got {gpu!r}")
        lists: Dict[str, List[Any]] = {}
        for name in ("local_modules", "declarations"):
            value = data.get(name)
            if value is not None and not isinstance(value, list):
                raise ManifestError(f"the manifest's '{name}' must be a list, got {value!r}")
            lists[name] = value or []

        unknown: Set[str] = {k for k in data if k not in _MANIFEST_FIELDS}
        manifest = cls(
            python_version=dict(python_version),
            packages=[ManifestPackage.from_dict(p, unknown) for p in packages],
            gpu=gpu,
            generated_at=_str_or_none(data.get("generated_at"), "generated_at") or "",
            tool_version=tool_version,
            schema_version=data["schema_version"],
            dependency_hash=_str_or_none(data.get("dependency_hash"), "dependency_hash") or "",
            local_modules=[LocalModule.from_dict(m, unknown) for m in lists["local_modules"]],
            declarations=[Declaration.from_dict(d, unknown) for d in lists["declarations"]],
            baseline=Baseline.from_dict(data.get("baseline")),
        )
        manifest.verified_hash = _manifest_payload_hash(data)
        manifest.unknown_fields = tuple(sorted(unknown))
        return manifest

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tool_version": self.tool_version,
            "generated_at": self.generated_at,
            "dependency_hash": self.dependency_hash,
            "python_version": self.python_version,
            "gpu": self.gpu,
            "baseline": self.baseline.to_dict() if self.baseline is not None else None,
            "packages": [p.to_dict() for p in self.packages],
            "local_modules": [m.to_dict() for m in self.local_modules],
            "declarations": [d.to_dict() for d in self.declarations],
        }

    def compute_and_set_hash(self) -> str:
        """Hashes canonical (sorted-key) JSON of every field except dependency_hash itself, so
        hand-editing anything in the manifest, generated_at included, invalidates the hash."""
        self.dependency_hash = _manifest_payload_hash(self.to_dict())
        return self.dependency_hash


@dataclass
class NotebookAnalysisReport:
    """Standardized single-notebook analysis report object."""
    notebook_path: str
    is_python: bool
    lang_label: str
    parse_error: Optional[str] = None
    dependencies: List[DependencyEntry] = field(default_factory=list)
    local_modules: List[str] = field(default_factory=list)
    platform_pseudo_modules: List[str] = field(default_factory=list)
    build_and_packaging_tools: List[str] = field(default_factory=list)
    gpu: Optional[GpuInfo] = None
    warnings: List[DiagnosticEvent] = field(default_factory=list)
    notices: List[DiagnosticEvent] = field(default_factory=list)
    promotions: List[PromotionDetail] = field(default_factory=list)
    local_tagged: List[Tuple[str, List[str]]] = field(default_factory=list)  # specific package builds; feeds the blueprint, not the JSON
    install_lines: List[InstallLine] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "notebook_path": self.notebook_path,
            "is_python": self.is_python,
            "lang_label": self.lang_label,
            "parse_error": self.parse_error,
            "dependencies": [d.to_report_dict() for d in self.dependencies],
            "local_modules": self.local_modules,
            "platform_pseudo_modules": self.platform_pseudo_modules,
            "build_and_packaging_tools": self.build_and_packaging_tools,
            "gpu": self.gpu.to_dict() if self.gpu else None,
            "warnings": [w.to_dict() for w in self.warnings],
            "notices": [n.to_dict() for n in self.notices],
            "promotions": [p.to_dict() for p in self.promotions],
            "install_lines": [line.to_dict() for line in self.install_lines],
        }


@dataclass
class ExtractionResult:
    """Encapsulates the raw extraction payload from reading a notebook file."""
    success: bool
    lang_label: str
    imports: List[str] = field(default_factory=list)
    submodules: Dict[str, Set[str]] = field(default_factory=dict)
    cells: List[Cell] = field(default_factory=list)
    error_msg: Optional[str] = None
    guarded_imports: Set[str] = field(default_factory=set)
    dynamic_warnings: List[DiagnosticEvent] = field(default_factory=list)
    writefile_imports: List[str] = field(default_factory=list)

    def __iter__(self) -> Iterator[Any]:
        """Legacy tuple-unpacking fallback for backward compatibility."""
        dyn_warn_strings = [w.format_console() for w in self.dynamic_warnings]
        return iter((
            self.success,
            self.imports,
            self.submodules,
            [c.source for c in self.cells],
            self.error_msg,
            self.lang_label,
            self.guarded_imports,
            dyn_warn_strings,
        ))


@dataclass
class HarvestResult:
    """Encapsulates harvested packages, index URLs, scoped flags, warnings, and notices from cell magics."""
    harvested_packages: Set[str] = field(default_factory=set)
    base_index_urls: Set[str] = field(default_factory=set)
    extra_index_urls: Set[str] = field(default_factory=set)
    magic_warnings: List[DiagnosticEvent] = field(default_factory=list)
    magic_notices: List[DiagnosticEvent] = field(default_factory=list)
    scoped_flags: Dict[str, List[str]] = field(default_factory=dict)
    raw_installs: List[RawInstall] = field(default_factory=list)
    install_lines: List[InstallLine] = field(default_factory=list)

    def __iter__(self) -> Iterator[Any]:
        """Legacy tuple-unpacking fallback for backward compatibility."""
        warn_strings = [w.format_console() for w in self.magic_warnings]
        notice_strings = [n.format_console() for n in self.magic_notices]
        return iter((
            self.harvested_packages,
            self.base_index_urls,
            self.extra_index_urls,
            warn_strings,
            notice_strings,
        ))


@dataclass
class BatchAnalysisSummary:
    """Aggregated analysis metrics across all notebooks in a batch repo scan."""
    target_dir: str
    total_python_notebooks: int = 0
    non_python_count: int = 0
    non_python_languages: Dict[str, int] = field(default_factory=dict)
    companion_skipped_count: int = 0
    parse_errors: List[Dict[str, str]] = field(default_factory=list)
    matched_packages: Set[str] = field(default_factory=set)    
    missing_packages: Dict[str, List[str]] = field(default_factory=dict)
    guarded_packages: Dict[str, List[str]] = field(default_factory=dict)
    local_packages: Dict[str, List[str]] = field(default_factory=dict)  # installed here but unshareable (editable, local file, custom path)
    promotions: List[PromotionDetail] = field(default_factory=list)
    dynamic_warnings: List[DiagnosticEvent] = field(default_factory=list)
    magic_warnings: List[DiagnosticEvent] = field(default_factory=list)
    magic_notices: List[DiagnosticEvent] = field(default_factory=list)
    batch_hardware_warnings: Dict[str, List[str]] = field(default_factory=dict)
    primary_url: Optional[str] = None
    primary_url_reason: Optional[str] = None
    batch_hw_cache: Optional[GpuInfo] = None
    notebooks: List[NotebookAnalysisReport] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return len(self.parse_errors) == 0


@dataclass
class DriftFinding:
    """One drift-check finding for a single pinned dependency.

    severity separates "confirmed" (yanked, removed, declared conflict) from
    "heuristic" (stale, major-bump) per the report's required visual split,
    plus "error" for a pin that couldn't be checked at all (never collapsed
    into "no drift found").
    """
    package: str
    version: str
    signal: str  # "conflict" | "yanked" | "removed" | "stale" | "major_bump" | "unsupported_python" | "check_error" | ...
    severity: str  # "confirmed" | "heuristic" | "error" | "notice" (a known custom source; see classify_against_baseline)
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    # Set only on a check-drift finding, and only when the manifest carries a usable baseline:
    # "known" (present at generation), "new", or "not_checked_at_generation".
    baseline_status: Optional[str] = None
    # Facts that identify a finding (they feed finding_baseline_key), so explicit fields rather
    # than entries in the display-only `details` bag. Both still appear under "details" in JSON.
    latest_version: Optional[str] = None  # major_bump: the newest version on PyPI
    parent: Optional[str] = None  # conflict: the package whose requirement conflicted ("" = a direct pin)

    def to_dict(self) -> Dict[str, Any]:
        details = dict(self.details)
        if self.latest_version is not None:
            details["latest_version"] = self.latest_version
        if self.parent is not None:
            details["parent"] = self.parent
        out = {
            "package": self.package,
            "version": self.version,
            "signal": self.signal,
            "severity": self.severity,
            "message": self.message,
            "details": details,
            "key": list(finding_identity_key(self)),
        }
        if self.baseline_status is not None:
            out["baseline_status"] = self.baseline_status
        return out


# Per-release facts: fixed for a given package version.
_PER_VERSION_SIGNALS = frozenset({
    Signal.YANKED, Signal.REMOVED, Signal.NOT_FOUND_ON_PYPI, Signal.UNSUPPORTED_PYTHON,
    Signal.UNVERIFIABLE_CUSTOM_INDEX,
})


def finding_baseline_key(finding: DriftFinding) -> Optional[FindingKey]:
    """The facts that make this finding 'the same problem' across runs; None if it has no
    generation-time counterpart (tamper, local-module and check-error findings)."""
    signal = finding.signal
    if signal in _PER_VERSION_SIGNALS:
        return (signal, finding.package, finding.version)
    if signal == Signal.STALE:
        return (signal, finding.package)  # the day count changes every run; the problem doesn't
    if signal == Signal.MAJOR_BUMP:
        latest = finding.latest_version or ""
        try:
            major = str(Version(latest).major)
        except InvalidVersion:
            major = latest
        return (signal, finding.package, major)  # a still-newer major is a different finding
    if signal == Signal.CONFLICT:
        return (signal, finding.package, finding.version, finding.parent or "")
    return None


def finding_identity_key(finding: DriftFinding) -> FindingKey:
    """A stable identity for any finding, for comparing two reports without touching messages or
    dates. Equal to the baseline key wherever one exists; findings with no baseline counterpart
    (tamper, local-module, check-error) fall back to signal, package and version."""
    return finding_baseline_key(finding) or (finding.signal, finding.package, finding.version)
