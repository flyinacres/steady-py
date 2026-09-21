"""Option and result types for the steady-py endpoints: scan, snapshot and check.

Pure data: no I/O, no printing, no exiting. The endpoints (`steady_py.endpoints`) build these,
the CLI (`steady_py.cli`) formats them and maps them to exit codes, and tests assert on them
directly. The types from `steady_py.core` are referenced for annotations only, so importing this
module does not load the analysis code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

if TYPE_CHECKING:
    from steady_py import core

# Plain string constants, not Enum members, for the same reason as core.StatusLabel: they serialize
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
    raw_full_freeze: List[str] = field(default_factory=list)  # full pip freeze lines


@dataclass(frozen=True)
class ScanOptions:
    """Options for scan."""
    suffix: Optional[str] = None  # marks generated companion files, which a directory scan skips; None means "_merged"


@dataclass(frozen=True)
class SnapshotOptions:
    """How snapshot delivers its output. The defaults return the cells and write nothing."""
    write_mode: str = WriteMode.NONE
    suffix: Optional[str] = None      # None means "_merged" for COMPANION and "" for DIRECTORY
    output_dir: Optional[str] = None  # required for, and only valid with, DIRECTORY
    universal: Optional[str] = None   # directory targets only: file name of the combined requirements file
    full_freeze: bool = False         # append the full environment freeze after the targeted pins
    install_timeout: int = 120        # per-package pip timeout baked into the generated Cell 2

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


@dataclass
class Delta:
    """What a snapshot would change in a notebook that already carries a manifest: the existing
    manifest (before) against a freshly computed one (after).

    Version changes are kept apart from added and removed packages because pins come from the
    environment the tool runs in. Run elsewhere, they show differences that reflect the machine,
    not the code.
    """
    added: List[PackageChange] = field(default_factory=list)
    removed: List[PackageChange] = field(default_factory=list)
    version_changes: List[PackageChange] = field(default_factory=list)
    python_version: Optional[Tuple[str, str]] = None     # (before, after), None when unchanged
    gpu: Optional[Tuple[GpuSetting, GpuSetting]] = None  # (before, after), None when unchanged
    baseline_compared: bool = False  # False when the fresh manifest has no baseline, as in scan, which never contacts PyPI
    findings_appeared: List[core.FindingKey] = field(default_factory=list)  # baseline findings; empty unless baseline_compared
    findings_resolved: List[core.FindingKey] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.added or self.removed or self.version_changes
            or self.python_version or self.gpu
            or self.findings_appeared or self.findings_resolved
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_changes": self.has_changes,
            "added": [c.to_dict() for c in self.added],
            "removed": [c.to_dict() for c in self.removed],
            "version_changes": [c.to_dict() for c in self.version_changes],
            "python_version": {"before": self.python_version[0], "after": self.python_version[1]} if self.python_version else None,
            "gpu": {"before": self.gpu[0], "after": self.gpu[1]} if self.gpu else None,
            "baseline_compared": self.baseline_compared,
            "findings_appeared": [list(k) for k in self.findings_appeared],
            "findings_resolved": [list(k) for k in self.findings_resolved],
        }


# =====================================================================
# PER-NOTEBOOK RESULTS
# =====================================================================
# `error` means the tool could not do its job for that notebook (unparseable file, a write that
# failed). It is None on success, including when there is nothing to report.

@dataclass
class NotebookScan:
    """scan of one notebook: what it needs."""
    path: str
    report: core.NotebookAnalysisReport
    manifest: Optional[core.SteadyPyManifest] = None  # the manifest the file already carries, if any
    manifest_error: Optional[str] = None              # the file has a manifest that could not be read
    delta: Optional[Delta] = None                     # set when `manifest` is present
    error: Optional[str] = None


@dataclass
class SetupCells:
    """The two generated cells."""
    markdown: str  # Cell 1
    code: str      # Cell 2, which carries the manifest literal


@dataclass
class NotebookSnapshot:
    """snapshot of one notebook: the setup cells and manifest, and where they went."""
    path: str
    report: core.NotebookAnalysisReport
    cells: Optional[SetupCells] = None
    drift_report: Optional[core.DriftCheckReport] = None  # generation-time validation of the new pins
    delta: Optional[Delta] = None                         # set when the file already had a manifest
    written_path: Optional[str] = None                    # None when nothing was written
    error: Optional[str] = None

    @property
    def manifest(self) -> Optional[core.SteadyPyManifest]:
        """The new manifest."""
        return self.drift_report.manifest if self.drift_report is not None else None


@dataclass
class NotebookCheck:
    """check of one notebook: its manifest against live PyPI."""
    path: str
    manifest_found: bool = False                       # True only when a manifest was found and read; False for
                                                       # 'nothing to check' (not an error) or an unreadable one (`error`)
    report: Optional[core.DriftCheckReport] = None
    error: Optional[str] = None                        # the manifest could not be read; a pin that could
                                                       # not be checked is in report.has_errors instead


# =====================================================================
# PER-TARGET RESULTS
# =====================================================================
# A file is a target with one notebook; a directory is a target with many. `notebooks` holds
# every notebook attempted, so a partial run is visible as some entries having an `error`.

@dataclass
class ScanResult:
    target: str
    kind: str = TargetKind.FILE
    notebooks: List[NotebookScan] = field(default_factory=list)
    batch_summary: Optional[core.BatchAnalysisSummary] = None  # directories only

    @property
    def failed(self) -> List[NotebookScan]:
        return [n for n in self.notebooks if n.error is not None]


@dataclass
class SnapshotResult:
    target: str
    kind: str = TargetKind.FILE
    notebooks: List[NotebookSnapshot] = field(default_factory=list)
    batch_summary: Optional[core.BatchAnalysisSummary] = None  # directories only
    validation: Optional[core.BatchValidation] = None         # aggregate validation across written notebooks
    universal_path: Optional[str] = None                      # the combined requirements file, if written
    error: Optional[str] = None                               # a failure of the run as a whole, not of one notebook

    @property
    def failed(self) -> List[NotebookSnapshot]:
        return [n for n in self.notebooks if n.error is not None]


@dataclass
class CheckResult:
    target: str
    kind: str = TargetKind.FILE
    notebooks: List[NotebookCheck] = field(default_factory=list)
    validation: Optional[core.BatchValidation] = None  # aggregate validation, directories only

    @property
    def failed(self) -> List[NotebookCheck]:
        return [n for n in self.notebooks if n.error is not None]
