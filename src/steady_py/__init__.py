"""steady-py: reproducible environment setup and drift checking for notebooks and Python projects."""
import logging

# Every module logs through a child of this logger (steady_py.<module>). It is set up here because this
# file runs before any submodule is imported. Importing the package must not touch process-global state
# (the standard streams, other loggers' handlers), so it only gets a NullHandler; the CLI entry point
# (cli.main) calls configure_console() to attach the stderr handler and force UTF-8 output. It never
# propagates to the root logger, so a host that configures logging (an IPython session, a test runner)
# does not print every message twice.
_logger = logging.getLogger("steady_py")
_logger.setLevel(logging.INFO)
_logger.propagate = False
if not _logger.handlers:  # guarded: a reload in a live kernel re-runs this file
    _logger.addHandler(logging.NullHandler())

from steady_py.runtime import install, InstallResult
from steady_py.endpoints import check, scan, snapshot
from steady_py.results import (
    CheckOptions,
    CheckResult,
    Delta,
    Environment,
    NotebookCheck,
    NotebookScan,
    NotebookSnapshot,
    PackageChange,
    ScanOptions,
    ScanResult,
    SetupCells,
    SnapshotOptions,
    SnapshotResult,
    TargetKind,
    WriteMode,
)

__all__ = [
    "check", "CheckOptions", "CheckResult", "Delta", "Environment", "install", "InstallResult", "NotebookCheck",
    "NotebookScan", "NotebookSnapshot", "PackageChange", "scan", "ScanOptions", "ScanResult", "SetupCells",
    "snapshot", "SnapshotOptions", "SnapshotResult", "TargetKind", "WriteMode",
]
