"""The endpoints: snapshot and check.

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
from typing import List, Optional, Sequence, Tuple

from steady_py import accelerator, analyze, constants, declarations, delta, drift, generate, installed, localmodules, models, scanning, util
from steady_py.results import (
    CheckOptions, CheckResult, Environment, NotebookCheck, NotebookSnapshot, SetupCells, SnapshotOptions, SnapshotResult,
    TargetKind, WriteMode,
)

DEFAULT_COMPANION_SUFFIX = "_merged"


def detect_environment() -> Environment:
    """The environment this process is running in."""
    frozen_env = installed.read_installed()
    pkg_dist_map = importlib.metadata.packages_distributions() if hasattr(importlib.metadata, "packages_distributions") else {}
    return Environment(frozen_env=frozen_env, pkg_dist_map=pkg_dist_map,
                       raw_full_freeze=sorted(frozen_env.values(), key=str.lower), editables=installed.read_editables())


def _read_manifest(path: object) -> generate.ManifestRead:
    """The manifest a notebook file already carries; an empty read when it has none or is not a file."""
    if not os.path.isfile(str(path)):
        return generate.ManifestRead()
    return generate.read_manifest_file(str(path))


@dataclass
class _Analysis:
    """The one analysis of a notebook that snapshot builds on."""
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
        ext_res = scanning.extract_from_active_session()
        path, kind, root_dir = Path("session.ipynb"), TargetKind.SESSION, "."

    hardware = accelerator.inspect_gpu_environment(list(dict.fromkeys(ext_res.imports)))
    scan_result = analyze.build_scan_result(path, ext_res)
    report = analyze.build_single_notebook_report(
        scan_result, environment.frozen_env, environment.pkg_dist_map, hardware, root_dir=root_dir,
        editables=environment.editables,
    )
    if kind == TargetKind.FILE:  # a live session's kernel is the notebook's own environment
        third_party = [d for d in report.dependencies if d.status in
                       {constants.DependencyStatus.PINNED, constants.DependencyStatus.DIRECT_REFERENCE,
                        constants.DependencyStatus.AMBIGUOUS, *constants.UNSHAREABLE_STATUSES}]
        missing = sum(1 for d in third_party if d.status == constants.DependencyStatus.PINNED and d.is_comment)
        warning = analyze.other_interpreter_warning(missing, len(third_party) - missing)
        if warning:
            warning.notebook = str(path)
            report.warnings.append(warning)
    return _Analysis(
        path=str(path), kind=kind, report=report, environment=environment,
        scan_result=scan_result, hardware=hardware, root_dir=root_dir,
    )


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
    # Read once for every notebook; an unusable file raises DeclarationError before anything runs.
    file_entries = declarations.from_file(options.declarations_file) if options.declarations_file else []
    if target is None and options.write_mode != WriteMode.NONE:
        raise ValueError("the live IPython session has no file to write into; use write_mode 'none'")
    if target is not None and os.path.isdir(target):
        return _snapshot_directory(target, options, environment, file_entries)
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
        root_dir=analysis.root_dir, file_entries=file_entries, live=analysis.kind == TargetKind.SESSION,
    )
    return SnapshotResult(target=result_target, kind=analysis.kind, notebooks=[notebook])


def _snapshot_notebook(
    scan_result: analyze.NotebookScanResult,
    report: models.NotebookAnalysisReport,
    hardware: Optional[models.GpuInfo],
    options: SnapshotOptions,
    full_freeze_lines: Optional[List[str]],
    root_dir: str,
    file_entries: Sequence[models.Declaration] = (),
    live: bool = False,
) -> NotebookSnapshot:
    """The cells and manifest for one analyzed notebook, written as the options say.

    The manifest the notebook already carries dates the new one (delta.settle) and is what the delta
    compares against. One a newer steady-py wrote is never replaced: the notebook fails with the
    reason. One that can't be read otherwise is replaced, with a warning naming why.
    """
    read = _read_manifest(scan_result.path)   # before any write: an in-place snapshot replaces it
    if read.newer:
        return NotebookSnapshot(path=str(scan_result.path), report=report, error=read.error)
    if read.error is not None:
        report.warnings.append(models.DiagnosticEvent(
            type="unreadable_manifest", notebook=str(scan_result.path),
            detail=f"{read.error} Snapshot builds a new manifest in its place; nothing from the old one is carried forward."))
    previous = read.manifest
    declared = declarations.read(scan_result.cells, file_entries, live=live)
    if declared.error is not None:
        return NotebookSnapshot(path=str(scan_result.path), report=report, error=declared.error)
    for warning in declared.warnings:
        warning.notebook = str(scan_result.path)
    report.warnings.extend(declared.warnings)
    blueprint = generate.build_blueprint_for_notebook(
        scan_result, report, hardware, install_timeout=options.install_timeout, full_freeze_lines=full_freeze_lines,
        previous=previous, declarations=declared.entries,
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
            if written is None:
                notebook.unchanged = True
            else:
                notebook.written_path = str(written)
        except OSError as exc:
            notebook.error = f"could not write the locked notebook: {exc}"
    return notebook


# --- directories -----------------------------------------------------------------------------

@dataclass
class _DirectoryAnalysis:
    """The one analysis of a directory of notebooks that snapshot builds on."""
    repo_map: analyze.RepoEnvironmentMap
    summary: models.BatchAnalysisSummary
    environment: Environment
    hardware: Optional[models.GpuInfo]


def _analyze_directory(target: str, environment: Optional[Environment], skip_suffix: Optional[str]) -> _DirectoryAnalysis:
    environment = environment or detect_environment()
    repo_map = analyze.walk_and_scan_directory(target, skip_suffix=skip_suffix)
    hardware = accelerator.inspect_gpu_environment(list(dict.fromkeys(repo_map.global_imports)))
    summary = analyze.analyze_batch_repository(repo_map, environment.frozen_env, environment.pkg_dist_map, hardware,
                                               editables=environment.editables)
    warning = analyze.other_interpreter_warning(len(summary.missing_packages),
                                                len(summary.matched_packages) + len(summary.local_packages))
    if warning:
        summary.dynamic_warnings.append(warning)
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


def _snapshot_directory(target: str, options: SnapshotOptions, environment: Optional[Environment],
                        file_entries: Sequence[models.Declaration] = ()) -> SnapshotResult:
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
                generate.generate_universal_manifest(repo_map, analysis.environment.frozen_env, analysis.environment.pkg_dist_map, skipped,
                                                    editables=analysis.environment.editables),
                encoding="utf-8",
            )
            result.universal_path = str(out_file)
        except OSError as exc:
            result.error = f"could not write the universal manifest: {exc}"

    full_freeze_lines = analysis.environment.raw_full_freeze if options.full_freeze else None
    validation_reports: List[Tuple[str, drift.DriftCheckReport]] = []
    for res, report in zip(repo_map.scan_results, summary.notebooks):
        notebook = _snapshot_notebook(res, report, analysis.hardware, options, full_freeze_lines, root_dir=repo_map.target_dir,
                                      file_entries=file_entries)
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
    checked too, since they are where the manifests are. Without `root_dir`, local modules are
    re-verified under the project root snapshot derived (localmodules.project_root), with the
    directory as the fallback for a directory run.
    """
    options = options or CheckOptions()
    util.reset_run_caches()
    if os.path.isdir(target):
        return _check_directory(target, options)
    return CheckResult(target=target, kind=TargetKind.FILE, notebooks=[_check_file(target, options)])


def _check_directory(target: str, options: CheckOptions) -> CheckResult:
    notebooks = [_check_file(str(path), options, root_fallback=target) for path in analyze.iter_notebook_paths(target)]
    reports = [(util.relative_notebook_path(Path(n.path), target), n.report) for n in notebooks if n.report is not None]
    validation = drift.build_batch_validation(reports) if reports else None
    return CheckResult(target=target, kind=TargetKind.DIRECTORY, notebooks=notebooks, validation=validation)


def _check_file(path: str, options: CheckOptions, root_fallback: Optional[str] = None) -> NotebookCheck:
    read = generate.read_manifest_file(path)
    if read.error:
        return NotebookCheck(path=path, error=read.error)
    manifest = read.manifest
    if manifest is None:
        return NotebookCheck(path=path)

    findings: List[models.DriftFinding] = []

    # Fields a newer steady-py added: this one can't check them, so a clean result would claim more
    # than it knows (decision 9).
    if manifest.unknown_fields:
        findings.append(models.DriftFinding(
            package="", version="", signal=constants.Signal.UNREAD_FIELDS, severity=constants.Severity.CONFIRMED,
            message=f"The manifest in {path} was written by steady-py {manifest.tool_version or '(unknown version)'}, "
                    f"which records fields this steady-py ({constants.TOOL_VERSION}) can't check: "
                    f"{', '.join(manifest.unknown_fields)}. Upgrade steady-py to check them.",
            details={"fields": list(manifest.unknown_fields)},
        ))

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

    notebook_dir = str(Path(path).parent)
    root_dir = options.root_dir or localmodules.project_root(notebook_dir, root_fallback)  # as snapshot derived it
    findings.extend(localmodules.check_local_modules(manifest, notebook_dir=notebook_dir, root_dir=root_dir))
    findings.extend(drift.run_pin_checks(manifest.pins(), manifest.python_version))

    report = drift.build_drift_check_report(path, manifest, findings)
    return NotebookCheck(path=path, manifest_found=True, report=report)
