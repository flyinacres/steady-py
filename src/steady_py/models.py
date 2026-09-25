"""Data types shared across layers: diagnostics, dependency entries, the manifest and its pins, the
generation-time baseline, drift findings and their identity keys, and the analysis reports."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from packaging.version import InvalidVersion, Version

from steady_py.constants import MANIFEST_SCHEMA_VERSION, TOOL_VERSION, DependencyStatus, Signal


@dataclass
class DiagnosticEvent:
    """Represents a structured warning or informational notice."""
    type: str
    detail: str
    cell_idx: Optional[int] = None
    line_idx: Optional[int] = None
    level: str = "warning"

    def format_console(self) -> str:
        prefix = "⚠️" if self.level == "warning" else "ℹ️"
        return f"{prefix} {self.detail}"

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
    """
    cell_idx: int
    line_idx: int
    module: str
    full_name: str = ""
    is_guarded: bool = False


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
            bakes in the creator's directory structure (see SteadyPyManifest.local_modules).
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

    def to_pin(self) -> "PinnedDependency":
        """This entry as a manifest pin (only meaningful for an installable, non-comment entry)."""
        return PinnedDependency(name=self.name, version=self.version, flags=tuple(self.flags))

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
    """
    dependencies: List[DependencyEntry] = field(default_factory=list)
    promotion_notices: List[PromotionDetail] = field(default_factory=list)
    conflict_warnings: List[DiagnosticEvent] = field(default_factory=list)

    def __iter__(self):
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
    """One pinned direct dependency as recorded in the manifest and consumed by the drift checks.

    `name` may carry an extras tag ("pandas[test]"). Only the persisted manifest literal uses the
    dict form (to_dict / from_dict); everything in memory uses this type.
    """
    name: str
    version: str
    flags: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "version": self.version, "flags": list(self.flags)}

    @classmethod
    def from_dict(cls, data: Any) -> "PinnedDependency":
        if not isinstance(data, dict):
            raise TypeError(f"a pinned dependency must be a dict, not {type(data).__name__}")
        name, version = data.get("name"), data.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise TypeError(f"a pinned dependency needs a string 'name' and 'version', got {data!r}")
        return cls(name=name, version=version, flags=tuple(data.get("flags") or ()))


def _manifest_payload_hash(payload: Dict[str, Any]) -> str:
    """SHA-256 of canonical (sorted-key) JSON of every field except dependency_hash itself."""
    body = {k: v for k, v in payload.items() if k != "dependency_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class SteadyPyManifest:
    """Reproducibility manifest embedded in generated Cell 2 as STEADY_PY_MANIFEST.

    Also the payload drift-check parses back out of a notebook/.py file to
    evaluate pins against live PyPI metadata (no execution, no installs).

    baseline records what generation-time validation found, as compact keys (see
    build_baseline), so a later check can tell findings that were already present from
    ones that are new. None means no baseline was recorded.

    local_modules records local sibling modules found at generation time, for
    drift-check to re-verify by existence (not a PyPI check -- these were never
    pip-installable). Each entry is {"name": ..., "anchor": "notebook_dir" |
    "root_dir"} -- never a path, so a shared notebook never bakes in the
    creator's directory structure.
    """
    python_version: Dict[str, int]
    dependencies: List[PinnedDependency]
    gpu: Optional[Dict[str, Any]]
    generated_at: str
    tool_version: str = TOOL_VERSION
    schema_version: str = MANIFEST_SCHEMA_VERSION
    dependency_hash: str = ""
    raw_installs: List[str] = field(default_factory=list)
    custom_sourced: List[str] = field(default_factory=list)
    local_modules: List[Dict[str, str]] = field(default_factory=list)
    baseline: Optional[Baseline] = None
    # Set only by from_literal: the hash recomputed over the fields exactly as they were
    # persisted. Never serialized, hashed or compared; it is not part of the manifest.
    verified_hash: Optional[str] = field(default=None, init=False, repr=False, compare=False)

    @classmethod
    def from_literal(cls, data: Dict[str, Any]) -> "SteadyPyManifest":
        """Builds a manifest from a persisted STEADY_PY_MANIFEST literal.

        The integrity hash is recomputed over `data` as found, not over this class's
        current field set. Adding a field to the manifest therefore never makes an older
        manifest look hand-edited, and deleting a field from a newer one is still caught.
        """
        if not isinstance(data, dict):
            raise TypeError(f"the manifest literal must be a dict, not {type(data).__name__}")
        deps = data.get("dependencies")
        if not isinstance(deps, list):
            raise TypeError("the manifest's 'dependencies' must be a list")
        manifest = cls(**{
            **data,
            "dependencies": [PinnedDependency.from_dict(d) for d in deps],
            "baseline": Baseline.from_dict(data.get("baseline")),
        })
        manifest.verified_hash = _manifest_payload_hash(data)
        return manifest

    def to_dict(self) -> Dict[str, Any]:
        return {
            "python_version": self.python_version,
            "dependencies": [d.to_dict() for d in self.dependencies],
            "gpu": self.gpu,
            "generated_at": self.generated_at,
            "tool_version": self.tool_version,
            "schema_version": self.schema_version,
            "dependency_hash": self.dependency_hash,
            "raw_installs": self.raw_installs,
            "custom_sourced": self.custom_sourced,
            "local_modules": self.local_modules,
            "baseline": self.baseline.to_dict() if self.baseline is not None else None,
        }

    def compute_and_set_hash(self) -> str:
        """Hashes canonical (sorted-key) JSON of every field except dependency_hash itself.

        Covers content and provenance fields alike, so hand-editing anything in
        the manifest -- including generated_at, to hide age -- invalidates the hash.
        """
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
        }


@dataclass
class ExtractionResult:
    """Encapsulates the raw extraction payload from reading a notebook file."""
    success: bool
    lang_label: str
    imports: List[str] = field(default_factory=list)
    submodules: Dict[str, Set[str]] = field(default_factory=dict)
    code_sources: List[str] = field(default_factory=list)
    error_msg: Optional[str] = None
    guarded_imports: Set[str] = field(default_factory=set)
    dynamic_warnings: List[DiagnosticEvent] = field(default_factory=list)
    writefile_imports: List[str] = field(default_factory=list)

    def __iter__(self):
        """Legacy tuple-unpacking fallback for backward compatibility."""
        dyn_warn_strings = [w.format_console() for w in self.dynamic_warnings]
        return iter((
            self.success,
            self.imports,
            self.submodules,
            self.code_sources,
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
    raw_installs: List[str] = field(default_factory=list)

    def __iter__(self):
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
