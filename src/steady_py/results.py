"""Option and result types for the steady-py endpoints: snapshot and check.

Pure data: no I/O, no printing, no exiting. The endpoints (`steady_py.endpoints`) build these,
the CLI (`steady_py.cli`) formats them and maps them to exit codes, and tests assert on them
directly. The types from `steady_py.models` and `steady_py.drift` are referenced for annotations
only, so importing this module does not load the analysis code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

if TYPE_CHECKING:
    from steady_py import drift, models

# Plain string constants, not Enum members, for the same reason as constants.StatusLabel: they serialize
# and format as ordinary strings on every supported Python version.


class TargetKind:
    """What an endpoint was pointed at."""
    FILE = "file"
    DIRECTORY = "directory"
    SESSION = "session"  # the live IPython kernel's history


class WriteMode:
    """Where snapshot delivers its output."""
    NONE = "none"            # return the cells, write nothing
    COMPANION = "companion"  # write <name><suffix>.ipynb beside the source; the source is untouched
    DIRECTORY = "directory"  # write into SnapshotOptions.output_dir
    IN_PLACE = "in_place"    # replace the setup cells and manifest in the source file


_WRITE_MODES = (WriteMode.NONE, WriteMode.COMPANION, WriteMode.DIRECTORY, WriteMode.IN_PLACE)

# What a manifest records for the accelerator (SteadyPyManifest.gpu): None, or a dict.
GpuSetting = Optional[Dict[str, Any]]


# =====================================================================
# INPUTS
# =====================================================================

@dataclass
class Environment:
    """The host environment an endpoint analyzes against.

    Endpoints detect it when not given one. Tests and embedding callers can pass a fixed one
    instead of patching the detection functions. The accelerator is not part of it: it is probed
    per target, from the imports found there.
    """
    frozen_env: Dict[str, str]                 # installed distribution -> its pin, e.g. 'numpy==2.0.1'
    pkg_dist_map: Mapping[str, List[str]]      # import name -> distributions that provide it
    raw_full_freeze: List[str] = field(default_factory=list)  # every installed pin, for --full-freeze
    editables: Mapping[str, str] = field(default_factory=dict)  # editable distribution -> its source directory


@dataclass(frozen=True)
class SnapshotOptions:
    """How snapshot delivers its output. The defaults return the cells and write nothing."""
    write_mode: str = WriteMode.NONE
    suffix: Optional[str] = None      # None means "_merged" for COMPANION and "" for DIRECTORY; a directory run
                                      # skips files carrying the companion suffix, since they are its own output
    output_dir: Optional[str] = None  # required for, and only valid with, DIRECTORY
    universal: Optional[str] = None   # directory targets only: file name of the combined requirements file
    full_freeze: bool = False         # append the full environment freeze after the targeted pins
    install_timeout: int = 120        # per-package pip timeout baked into the generated Cell 2
    declarations_file: Optional[str] = None  # a declarations file, applied before each notebook's own cell

    def __post_init__(self) -> None:
        if self.write_mode not in _WRITE_MODES:
            raise ValueError(f"unknown write_mode {self.write_mode!r}")
        if self.write_mode == WriteMode.DIRECTORY and not self.output_dir:
            raise ValueError("write_mode 'directory' needs output_dir")
        if self.output_dir and self.write_mode != WriteMode.DIRECTORY:
            raise ValueError("output_dir is only valid with write_mode 'directory'")


@dataclass(frozen=True)
class CheckOptions:
    """Options for check."""
    root_dir: Optional[str] = None  # anchor for re-verifying root_dir-anchored local modules


# =====================================================================
# DELTA
# =====================================================================

@dataclass(frozen=True)
class PackageChange:
    """One package that differs between two manifests. `old_version` is None for an added package,
    `new_version` is None for a removed one."""
    name: str
    old_version: Optional[str] = None
    new_version: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "old_version": self.old_version, "new_version": self.new_version}


@dataclass(frozen=True)
class FieldChange:
    """One manifest field that differs. `section` is "packages", "local_modules", "declarations" or
    "manifest" (a top-level field); `name` labels the entry ("" for a top-level field); `field` is
    the field, or "entry" for a local module or declaration added (before None) or removed (after
    None)."""
    section: str
    name: str
    field: str
    before: Any
    after: Any

    def to_dict(self) -> Dict[str, Any]:
        return {"section": self.section, "name": self.name, "field": self.field, "before": self.before, "after": self.after}


@dataclass
class Delta:
    """What a snapshot would change in a notebook that already carries a manifest: the existing
    manifest (before) against a freshly computed one (after), covering every manifest field (P3).

    Version changes are kept apart from added and removed packages because pins come from the
    environment the tool runs in. Run elsewhere, they show differences that reflect the machine,
    not the code.
    """
    added: List[PackageChange] = field(default_factory=list)
    removed: List[PackageChange] = field(default_factory=list)
    version_changes: List[PackageChange] = field(default_factory=list)
    field_changes: List[FieldChange] = field(default_factory=list)  # every other change, field by field
    python_version: Optional[Tuple[str, str]] = None     # (before, after), None when unchanged
    gpu: Optional[Tuple[GpuSetting, GpuSetting]] = None  # (before, after), None when unchanged
    baseline_compared: bool = False  # False when the fresh manifest has no baseline
    findings_appeared: List[models.FindingKey] = field(default_factory=list)  # baseline findings
    findings_resolved: List[models.FindingKey] = field(default_factory=list)
    unchecked_appeared: List[str] = field(default_factory=list)  # packages validation could not check
    unchecked_resolved: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.added or self.removed or self.version_changes or self.field_changes
            or self.python_version or self.gpu
            or self.findings_appeared or self.findings_resolved or self.unchecked_appeared or self.unchecked_resolved
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_changes": self.has_changes,
            "added": [c.to_dict() for c in self.added],
            "removed": [c.to_dict() for c in self.removed],
            "version_changes": [c.to_dict() for c in self.version_changes],
            "field_changes": [c.to_dict() for c in self.field_changes],
            "python_version": {"before": self.python_version[0], "after": self.python_version[1]} if self.python_version else None,
            "gpu": {"before": self.gpu[0], "after": self.gpu[1]} if self.gpu else None,
            "baseline_compared": self.baseline_compared,
            "findings_appeared": [list(k) for k in self.findings_appeared],
            "findings_resolved": [list(k) for k in self.findings_resolved],
            "unchecked_appeared": list(self.unchecked_appeared),
            "unchecked_resolved": list(self.unchecked_resolved),
        }


# =====================================================================
# PER-NOTEBOOK RESULTS
# =====================================================================
# `error` means the tool could not do its job for that notebook (unparseable file, a write that
# failed). It is None on success, including when there is nothing to report.

@dataclass
class SetupCells:
    """The two generated cells."""
    markdown: str  # Cell 1
    code: str      # Cell 2, which carries the manifest literal


@dataclass
class NotebookSnapshot:
    """snapshot of one notebook: the setup cells and manifest, and where they went."""
    path: str
    report: models.NotebookAnalysisReport
    cells: Optional[SetupCells] = None
    drift_report: Optional[drift.DriftCheckReport] = None  # generation-time validation of the new pins
    delta: Optional[Delta] = None                         # set when the file already had a manifest
    written_path: Optional[str] = None                    # None when nothing was written
    unchanged: bool = False                               # an in-place write found nothing to change and wrote nothing
    error: Optional[str] = None

    @property
    def manifest(self) -> Optional[models.SteadyPyManifest]:
        """The new manifest."""
        return self.drift_report.manifest if self.drift_report is not None else None


@dataclass
class NotebookCheck:
    """check of one notebook: its manifest against live PyPI."""
    path: str
    manifest_found: bool = False                       # True only when a manifest was found and read; False for
                                                       # 'nothing to check' (not an error) or an unreadable one (`error`)
    report: Optional[drift.DriftCheckReport] = None
    error: Optional[str] = None                        # the manifest could not be read; a pin that could
                                                       # not be checked is in report.has_errors instead


# =====================================================================
# PER-TARGET RESULTS
# =====================================================================
# A file is a target with one notebook; a directory is a target with many. `notebooks` holds
# every notebook attempted, so a partial run is visible as some entries having an `error`.

@dataclass
class SnapshotResult:
    target: str
    kind: str = TargetKind.FILE
    notebooks: List[NotebookSnapshot] = field(default_factory=list)
    batch_summary: Optional[models.BatchAnalysisSummary] = None  # directories only
    validation: Optional[drift.BatchValidation] = None         # aggregate validation across written notebooks
    universal_path: Optional[str] = None                      # the combined requirements file, if written
    error: Optional[str] = None                               # a failure of the run as a whole, not of one notebook
    skipped: Optional[str] = None                             # why a live snapshot returned without running

    @property
    def failed(self) -> List[NotebookSnapshot]:
        return [n for n in self.notebooks if n.error is not None]


@dataclass
class CheckResult:
    target: str
    kind: str = TargetKind.FILE
    notebooks: List[NotebookCheck] = field(default_factory=list)
    validation: Optional[drift.BatchValidation] = None  # aggregate validation, directories only

    @property
    def failed(self) -> List[NotebookCheck]:
        return [n for n in self.notebooks if n.error is not None]
