"""The endpoints: scan, snapshot and check.

Each one computes and returns a typed result (see results.py). None of them prints or exits;
that is the CLI's job. Each call is one run: it starts by clearing the per-run caches, so a second
call in the same process sees what changed since the first. They call package functions through
their defining module (`analyze.build_single_notebook_report(...)`), so a test that patches the
function on that module takes effect here.
"""
from __future__ import annotations

import importlib.metadata
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import sys

from steady_py import accelerator, analyze, constants, delta, drift, generate, installed, localmodules, models, scanning, util
from steady_py.results import (
    CheckOptions, CheckResult, Environment, NotebookCheck, NotebookScan, NotebookSnapshot, ScanOptions,
    ScanResult, SetupCells, SnapshotOptions, SnapshotResult, TargetKind, WriteMode,
)

DEFAULT_COMPANION_SUFFIX = "_merged"


def detect_environment() -> Environment:
    """The environment this process is running in."""
    frozen_env, raw_full_freeze = installed.get_installed_environment()
    pkg_dist_map = importlib.metadata.packages_distributions() if hasattr(importlib.metadata, "packages_distributions") else {}
    return Environment(frozen_env=frozen_env, pkg_dist_map=pkg_dist_map, raw_full_freeze=raw_full_freeze)


def _read_manifest(path: object) -> Tuple[Optional[models.SteadyPyManifest], Optional[str]]:
    """The manifest a notebook file already carries: (manifest, None), (None, None) when it has
    none or is not a file, and (None, reason) when it has one that cannot be read."""
    if not os.path.isfile(str(path)):
        return None, None
    manifest, error = generate.extract_manifest_from_file(str(path))
    return manifest, error or None


def _provisional_manifest(
    scan_result: analyze.NotebookScanResult, report: models.NotebookAnalysisReport, hardware: Optional[models.GpuInfo],
) -> models.SteadyPyManifest:
    """What a snapshot would record, as far as it can be known without contacting PyPI: the pins,
    the Python version and the accelerator. It has no baseline, custom-sourced list or hash."""
    gpu = accelerator.resolve_notebook_gpu_info(scan_result.imports, hardware)
    return models.SteadyPyManifest(
        python_version={"major": sys.version_info.major, "minor": sys.version_info.minor},
        dependencies=[dep.to_pin() for dep in report.dependencies if not dep.is_comment],
        gpu=gpu.to_dict() if gpu else None,
        generated_at="",
    )


def _scan_notebook(
    scan_result: analyze.NotebookScanResult, report: models.NotebookAnalysisReport, hardware: Optional[models.GpuInfo],
) -> NotebookScan:
    """One analyzed notebook, compared with the manifest it already carries, if any."""
    manifest, manifest_error = _read_manifest(scan_result.path)
    manifest_delta = delta.compute_delta(manifest, _provisional_manifest(scan_result, report, hardware)) if manifest else None
    return NotebookScan(
        path=str(scan_result.path), report=report, manifest=manifest, manifest_error=manifest_error, delta=manifest_delta,
    )


@dataclass
class _Analysis:
    """The one analysis of a notebook that scan and snapshot both build on."""
    path: str
    kind: str
    report: models.NotebookAnalysisReport
    environment: Environment
    scan_result: Optional[analyze.NotebookScanResult] = None  # None when the file could not be read
    hardware: Optional[models.GpuInfo] = None                # the probed accelerator
    root_dir: str = "."
    error: Optional[str] = None


def _analyze(target: Optional[str], environment: Optional[Environment]) -> _Analysis:
    """Reads a notebook file, or the live IPython session when `target` is None, and analyzes it once."""
    environment = environment or detect_environment()

    if target is not None:
        ext_res = scanning.extract_from_file(target, strict=False)
        if not ext_res.success:
            unreadable = models.NotebookAnalysisReport(
                notebook_path=str(target), is_python=False, lang_label=ext_res.lang_label, parse_error=ext_res.error_msg,
            )
            return _Analysis(path=target, kind=TargetKind.FILE, report=unreadable, environment=environment, error=ext_res.error_msg)
        path, kind, root_dir = Path(target), TargetKind.FILE, str(Path(target).parent)
    else:
        if not util.is_running_in_ipython():
            raise ValueError("a target file is required outside a live IPython session")
        imports, submodules, code_sources, guarded_imports, dynamic_warnings = scanning.extract_from_active_session()
        ext_res = models.ExtractionResult(
            success=True, lang_label=constants.StatusLabel.PYTHON, imports=imports, submodules=submodules,
            code_sources=code_sources, guarded_imports=guarded_imports, dynamic_warnings=dynamic_warnings,
            writefile_imports=scanning.extract_writefile_imports_from_sources(code_sources),
        )
        path, kind, root_dir = Path("session.ipynb"), TargetKind.SESSION, "."

    hardware = accelerator.inspect_gpu_environment(list(dict.fromkeys(ext_res.imports)))
    scan_result = analyze.build_scan_result(path, ext_res)
    report = analyze.build_single_notebook_report(
        scan_result, environment.frozen_env, environment.pkg_dist_map, hardware, root_dir=root_dir,
    )
    return _Analysis(
        path=str(path), kind=kind, report=report, environment=environment,
        scan_result=scan_result, hardware=hardware, root_dir=root_dir,
    )


def scan(
    target: Optional[str] = None,
    options: Optional[ScanOptions] = None,
    environment: Optional[Environment] = None,
) -> ScanResult:
    """Analyzes a notebook file, a directory of notebooks, or the live IPython session when `target`
    is None, and reports what it needs: its dependencies, warnings, notices and detected hardware.

    Read-only, and it never contacts PyPI.
    """
    options = options or ScanOptions()
    util.reset_run_caches()
    if target is not None and os.path.isdir(target):
        return _scan_directory(target, options, environment)
    analysis = _analyze(target, environment)
    if analysis.scan_result is None:
        notebook = NotebookScan(path=analysis.path, report=analysis.report, error=analysis.error)
    else:
        notebook = _scan_notebook(analysis.scan_result, analysis.report, analysis.hardware)
    return ScanResult(target=target if target is not None else analysis.path, kind=analysis.kind, notebooks=[notebook])


def snapshot(
    target: Optional[str] = None,
    options: Optional[SnapshotOptions] = None,
    environment: Optional[Environment] = None,
) -> SnapshotResult:
    """Analyzes a notebook file, a directory of notebooks, or the live IPython session when `target`
    is None, and produces the two setup cells and the manifest for each, validated against PyPI.

    With the default options nothing is written and the cells come back in the result. Otherwise
    they are also written, per `options.write_mode`. The cells are the same either way.
    """
    options = options or SnapshotOptions()
    util.reset_run_caches()
    if target is None and options.write_mode != WriteMode.NONE:
        raise ValueError("the live IPython session has no file to write into; use write_mode 'none'")
    if target is not None and os.path.isdir(target):
        return _snapshot_directory(target, options, environment)
    if options.universal:
        raise ValueError("universal is only valid for a directory target")
    analysis = _analyze(target, environment)
    result_target = target if target is not None else analysis.path
    if analysis.scan_result is None:
        failed = NotebookSnapshot(path=analysis.path, report=analysis.report, error=analysis.error)
        return SnapshotResult(target=result_target, kind=analysis.kind, notebooks=[failed])

    notebook = _snapshot_notebook(
        analysis.scan_result, analysis.report, analysis.hardware, options,
        full_freeze_lines=analysis.environment.raw_full_freeze if options.full_freeze else None,
        root_dir=analysis.root_dir,
    )
    return SnapshotResult(target=result_target, kind=analysis.kind, notebooks=[notebook])


def _snapshot_notebook(
    scan_result: analyze.NotebookScanResult,
    report: models.NotebookAnalysisReport,
    hardware: Optional[models.GpuInfo],
    options: SnapshotOptions,
    full_freeze_lines: Optional[List[str]],
    root_dir: str,
) -> NotebookSnapshot:
    """The cells and manifest for one analyzed notebook, written as the options say."""
    previous, _ = _read_manifest(scan_result.path)   # before any write: an in-place snapshot replaces it
    blueprint = generate.build_blueprint_for_notebook(
        scan_result, report, hardware, install_timeout=options.install_timeout, full_freeze_lines=full_freeze_lines,
    )
    notebook = NotebookSnapshot(
        path=str(scan_result.path),
        report=report,
        cells=SetupCells(markdown=blueprint["step1_markdown"], code=blueprint["step2_code"]),
        drift_report=blueprint["drift_report"],
        delta=delta.compute_delta(previous, blueprint["drift_report"].manifest) if previous else None,
    )
    if options.write_mode != WriteMode.NONE:
        try:
            written = generate.write_locked_notebook(
                scan_result, blueprint,
                suffix=options.suffix,
                in_place=options.write_mode == WriteMode.IN_PLACE,
                root_dir=root_dir,
                output_dir=options.output_dir,
            )
            notebook.written_path = str(written)
        except OSError as exc:
            notebook.error = f"could not write the locked notebook: {exc}"
    return notebook


# --- directories -----------------------------------------------------------------------------

@dataclass
class _DirectoryAnalysis:
    """The one analysis of a directory of notebooks that scan and snapshot both build on."""
    repo_map: analyze.RepoEnvironmentMap
    summary: models.BatchAnalysisSummary
    environment: Environment
    hardware: Optional[models.GpuInfo]


def _analyze_directory(target: str, environment: Optional[Environment], skip_suffix: Optional[str]) -> _DirectoryAnalysis:
    environment = environment or detect_environment()
    repo_map = analyze.walk_and_scan_directory(target, skip_suffix=skip_suffix)
    hardware = accelerator.inspect_gpu_environment(list(dict.fromkeys(repo_map.global_imports)))
    summary = analyze.analyze_batch_repository(repo_map, environment.frozen_env, environment.pkg_dist_map, hardware)
    return _DirectoryAnalysis(repo_map=repo_map, summary=summary, environment=environment, hardware=hardware)


def _skip_suffix(suffix: Optional[str], in_place: bool) -> Optional[str]:
    """The suffix that marks a generated companion file to skip while scanning. An in-place run
    writes no companions, so it skips none."""
    return None if in_place else (suffix if suffix is not None else DEFAULT_COMPANION_SUFFIX)


def _unreadable(repo_map: analyze.RepoEnvironmentMap) -> List[Tuple[str, models.NotebookAnalysisReport, str]]:
    """(path, report, cause) for each notebook that could not be read."""
    out = []
    for err in repo_map.parse_errors:
        cause = err.parse_error or "Unknown parse error"
        report = models.NotebookAnalysisReport(
            notebook_path=str(err.path), is_python=err.is_python, lang_label=err.lang_label, parse_error=cause,
        )
        out.append((str(err.path), report, cause))
    return out


def _scan_directory(target: str, options: ScanOptions, environment: Optional[Environment]) -> ScanResult:
    analysis = _analyze_directory(target, environment, _skip_suffix(options.suffix, in_place=False))
    notebooks = [
        _scan_notebook(res, report, analysis.hardware)
        for res, report in zip(analysis.repo_map.scan_results, analysis.summary.notebooks)
    ]
    notebooks += [NotebookScan(path=path, report=report, error=cause) for path, report, cause in _unreadable(analysis.repo_map)]
    return ScanResult(target=target, kind=TargetKind.DIRECTORY, notebooks=notebooks, batch_summary=analysis.summary)


def _snapshot_directory(target: str, options: SnapshotOptions, environment: Optional[Environment]) -> SnapshotResult:
    analysis = _analyze_directory(target, environment, _skip_suffix(options.suffix, options.write_mode == WriteMode.IN_PLACE))
    repo_map, summary = analysis.repo_map, analysis.summary
    result = SnapshotResult(target=target, kind=TargetKind.DIRECTORY, batch_summary=summary)
    unreadable = _unreadable(repo_map)

    # Partial writes: whatever parsed is processed and written, and the notebooks that did not are
    # listed in the result (and in the universal file), never silently dropped.
    if options.universal and repo_map.scan_results:
        skipped = [(util.relative_notebook_path(Path(path), repo_map.target_dir), cause) for path, _, cause in unreadable]
        out_file = Path(target) / options.universal
        try:
            out_file.write_text(
                generate.generate_universal_manifest(repo_map, analysis.environment.frozen_env, analysis.environment.pkg_dist_map, skipped),
                encoding="utf-8",
            )
            result.universal_path = str(out_file)
        except OSError as exc:
            result.error = f"could not write the universal manifest: {exc}"

    full_freeze_lines = analysis.environment.raw_full_freeze if options.full_freeze else None
    validation_reports: List[Tuple[str, drift.DriftCheckReport]] = []
    for res, report in zip(repo_map.scan_results, summary.notebooks):
        notebook = _snapshot_notebook(res, report, analysis.hardware, options, full_freeze_lines, root_dir=repo_map.target_dir)
        result.notebooks.append(notebook)
        if notebook.written_path is not None and notebook.drift_report is not None:
            validation_reports.append((util.relative_notebook_path(res.path, repo_map.target_dir), notebook.drift_report))
    result.notebooks += [NotebookSnapshot(path=path, report=report, error=cause) for path, report, cause in unreadable]
    if validation_reports:
        result.validation = drift.build_batch_validation(validation_reports)
    return result


def check(target: str, options: Optional[CheckOptions] = None) -> CheckResult:
    """Compares the manifest in a notebook, a .py file or every notebook under a directory with
    live PyPI: yanked or removed pins, conflicts, an unsupported Python, a hand-edited manifest,
    missing local modules.

    Read-only. A file with no manifest is not an error (a notebook from before manifests is a
    valid state): its result has `manifest_found` False and nothing to report. For a directory,
    the result also carries the aggregate validation section, and generated companion files are
    checked too, since they are where the manifests are. A directory is the default `root_dir`
    for local modules, the same root snapshot recorded them against.
    """
    options = options or CheckOptions()
    util.reset_run_caches()
    if os.path.isdir(target):
        return _check_directory(target, options)
    return CheckResult(target=target, kind=TargetKind.FILE, notebooks=[_check_file(target, options)])


def _check_directory(target: str, options: CheckOptions) -> CheckResult:
    options = CheckOptions(root_dir=options.root_dir or target)
    notebooks = [_check_file(str(path), options) for path in analyze.iter_notebook_paths(target)]
    reports = [(util.relative_notebook_path(Path(n.path), target), n.report) for n in notebooks if n.report is not None]
    validation = drift.build_batch_validation(reports) if reports else None
    return CheckResult(target=target, kind=TargetKind.DIRECTORY, notebooks=notebooks, validation=validation)


def _check_file(path: str, options: CheckOptions) -> NotebookCheck:
    manifest, error = generate.extract_manifest_from_file(path)
    if error:
        return NotebookCheck(path=path, error=error)
    if manifest is None:
        return NotebookCheck(path=path)

    findings: List[models.DriftFinding] = []

    # Verify the manifest hasn't been hand-edited since it was generated. Only meaningful here:
    # generation is writing dependency_hash for the first time, not verifying a prior one. The
    # stored hash stays on the manifest so the report shows what the file actually contains.
    stored_hash = manifest.dependency_hash
    recomputed_hash = manifest.verified_hash
    if recomputed_hash != stored_hash:
        findings.append(models.DriftFinding(
            package="", version="", signal=constants.Signal.TAMPERED, severity=constants.Severity.CONFIRMED,
            message=f"Manifest hash mismatch in {path} -- it may have been hand-edited since generation.",
            details={"stored_hash": stored_hash, "recomputed_hash": recomputed_hash},
        ))

    findings.extend(localmodules.check_local_modules(manifest, notebook_dir=str(Path(path).parent), root_dir=options.root_dir))
    findings.extend(drift.run_pin_checks(manifest.dependencies, manifest.python_version))

    report = drift.build_drift_check_report(path, manifest, findings)
    return NotebookCheck(path=path, manifest_found=True, report=report)
