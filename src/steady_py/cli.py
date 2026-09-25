"""The command line: formats endpoint results and maps them to exit codes.

Formatting and exit-code mapping are pure functions of a result, so they are tested without a
process, a network or a notebook on disk.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from steady_py import constants, core, endpoints, localmodules, models
from steady_py.results import (
    CheckOptions, CheckResult, Delta, Environment, NotebookCheck, NotebookScan, NotebookSnapshot, ScanOptions,
    ScanResult, SnapshotOptions, SnapshotResult, TargetKind, WriteMode,
)

logger = core.logger

EXIT_OK = 0
EXIT_ATTENTION = 1  # the tool did its job and found something that needs attention
EXIT_FAILED = 2     # the tool could not do its job


def _notebook_check_code(notebook: NotebookCheck) -> int:
    if notebook.error is not None:
        return EXIT_FAILED
    report = notebook.report
    if report is None:  # no manifest: nothing to check
        return EXIT_OK
    if report.has_errors:
        return EXIT_FAILED
    if report.has_confirmed or report.has_actionable_heuristic:
        return EXIT_ATTENTION
    return EXIT_OK


def check_exit_code(result: CheckResult) -> int:
    """0 clean, 1 drift found, 2 the manifest or a pin could not be checked.

    Drift is any confirmed finding, new or already known at generation, or a heuristic finding
    that was not already known. Across the notebooks of a directory: 2 only when none of them
    could be checked, otherwise 1 if any has drift or any could not be checked, otherwise 0. A
    notebook with no manifest has nothing to check and counts as clean.
    """
    codes = [_notebook_check_code(n) for n in result.notebooks]
    failed = codes.count(EXIT_FAILED)
    if not failed:
        return max(codes, default=EXIT_OK)
    return EXIT_ATTENTION if failed < len(codes) else EXIT_FAILED


def _notebook_check_json(notebook: NotebookCheck) -> str:
    """The JSON for one notebook: the drift report, or a small object saying why there is none."""
    if notebook.report is not None:
        return core.format_json_drift_report(notebook.report)
    return json.dumps({
        "schema_version": constants.SCHEMA_VERSION, "tool_version": constants.TOOL_VERSION, "mode": "check_drift",
        "target": notebook.path, "manifest_found": False, "error": notebook.error,
    }, indent=2)


def _format_check_directory(result: CheckResult, output_format: str) -> Tuple[str, str]:
    notebooks = result.notebooks
    with_manifest = [n for n in notebooks if n.report is not None]
    unreadable = [n for n in notebooks if n.error is not None]
    without = len(notebooks) - len(with_manifest) - len(unreadable)
    if output_format == "json":
        payload = {
            "schema_version": constants.SCHEMA_VERSION, "tool_version": constants.TOOL_VERSION, "mode": "check_batch",
            "target_dir": result.target,
            "summary": {"notebooks": len(notebooks), "with_manifest": len(with_manifest),
                        "without_manifest": without, "unreadable": len(unreadable)},
            "notebooks": [
                {"path": core.relative_notebook_path(Path(n.path), result.target), "manifest_found": n.report is not None,
                 "error": n.error, "drift_check": n.report.to_dict() if n.report is not None else None}
                for n in notebooks
            ],
            "validation": result.validation.to_dict() if result.validation else None,
        }
        errors = [f"⚠️ {core.relative_notebook_path(Path(n.path), result.target)}: {n.error}" for n in unreadable]
        return json.dumps(payload, indent=2), "\n".join(errors)
    if not notebooks:
        return f"No notebooks found in {result.target} -- nothing to check.", ""
    out = [f"Checked {len(notebooks)} notebook(s) in {result.target}: {len(with_manifest)} with a manifest, "
           f"{without} without (nothing to check), {len(unreadable)} could not be read."]
    if result.validation is not None:
        out += ["", core.format_console_batch_validation(result.validation)]
    errors = [f"⚠️ {core.relative_notebook_path(Path(n.path), result.target)}: {n.error}" for n in unreadable]
    return "\n".join(out), "\n".join(errors)


def format_check_result(result: CheckResult, output_format: str = "text") -> Tuple[str, str]:
    """(stdout text, stderr text) for a check result. Either may be empty. JSON mode always puts
    JSON on stdout, including for a notebook with no manifest or one that could not be read."""
    if result.kind == TargetKind.DIRECTORY:
        return _format_check_directory(result, output_format)
    out_parts, err_parts = [], []
    for notebook in result.notebooks:
        if notebook.error is not None:
            err_parts.append(f"⚠️ {notebook.error}")
        if output_format == "json":
            out_parts.append(_notebook_check_json(notebook))
        elif notebook.error is not None:
            continue
        elif notebook.report is None:
            out_parts.append(f"No STEADY_PY_MANIFEST found in {notebook.path} -- nothing to check.")
        else:
            out_parts.append(core.format_console_drift_report(notebook.report))
    return "\n".join(out_parts), "\n".join(err_parts)


def run_check(target: str, output_format: str = "text", root_dir: Optional[str] = None) -> int:
    """check, printed: runs the endpoint, prints the report and returns the exit code."""
    result = endpoints.check(target, CheckOptions(root_dir=root_dir))
    out, err = format_check_result(result, output_format)
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    return check_exit_code(result)


# --- scan and snapshot, for one notebook file or a directory ------------------------------------

def _run_exit_code(result: Union[ScanResult, SnapshotResult]) -> int:
    """0 when everything was processed; 1 when some of it was and some was not (an unreadable
    notebook, a failed write); 2 when nothing could be processed."""
    failed = len(result.failed)
    processed = len(result.notebooks) - failed
    run_error = getattr(result, "error", None) is not None
    if not failed and not run_error:
        return EXIT_OK
    return EXIT_ATTENTION if processed else EXIT_FAILED


def scan_exit_code(result: ScanResult) -> int:
    """0, 1 or 2 by how much of the target could be read. One unreadable file is 2, as is a
    directory where nothing could be read; a directory where only some could be is 1."""
    return _run_exit_code(result)


def snapshot_exit_code(result: SnapshotResult) -> int:
    """0, 1 or 2 by how much of the target could be processed and written; see scan_exit_code.
    A failure of the run as a whole, such as the universal file, counts like a failed notebook."""
    return _run_exit_code(result)


def _gpu_label(setting: Optional[Dict[str, Any]]) -> str:
    if not setting:
        return "none"
    return str(setting.get("device_name") or "GPU") if setting.get("has_gpu") else "no GPU"


def format_delta(delta: Delta, label: str) -> str:
    """What a snapshot would change (or did change) in the manifest a notebook already carried."""
    if not delta.has_changes:
        return f"No changes since the existing manifest in {label}."
    lines = [f"Changes since the existing manifest in {label}:"]
    lines += [f"  + {c.name}=={c.new_version} (added)" for c in delta.added]
    lines += [f"  - {c.name}=={c.old_version} (removed)" for c in delta.removed]
    lines += [f"  ~ {c.name} {c.old_version} -> {c.new_version}" for c in delta.version_changes]
    if delta.python_version:
        lines.append(f"  Python {delta.python_version[0]} -> {delta.python_version[1]}")
    if delta.gpu:
        lines.append(f"  Accelerator: {_gpu_label(delta.gpu[0])} -> {_gpu_label(delta.gpu[1])}")
    if delta.findings_appeared:
        lines.append("  New findings: " + ", ".join(":".join(key) for key in delta.findings_appeared))
    if delta.findings_resolved:
        lines.append("  Resolved findings: " + ", ".join(":".join(key) for key in delta.findings_resolved))
    if delta.version_changes:
        lines.append("  Versions come from the environment this ran in, so a different machine can show version changes the code did not cause.")
    return "\n".join(lines)


def _directory_deltas(result: Union[ScanResult, SnapshotResult]) -> Dict[str, Delta]:
    """The delta of each notebook that already had a manifest, keyed by path relative to the target."""
    return {
        core.relative_notebook_path(Path(n.path), result.target): n.delta
        for n in result.notebooks if n.delta is not None
    }


def format_directory_deltas(result: Union[ScanResult, SnapshotResult]) -> str:
    return "\n\n".join(format_delta(delta, rel) for rel, delta in _directory_deltas(result).items())


def format_scan_result(result: ScanResult) -> str:
    """The JSON report for a scanned notebook."""
    notebook = result.notebooks[0]
    return core.format_json_single_report(notebook.report, delta=notebook.delta.to_dict() if notebook.delta else None)


def format_snapshot_result(result: SnapshotResult, output_format: str = "text") -> str:
    """What snapshot prints on stdout. Text is the two cells to paste when nothing was written,
    and the validation report otherwise, each after the delta when the notebook already had a
    manifest; JSON is the analysis report, with the delta, and with the validation report and
    the written path when something was written."""
    notebook = result.notebooks[0]
    written = notebook.written_path is not None
    if output_format == "json":
        return core.format_json_single_report(
            notebook.report,
            artifacts_written={"locked_notebook": notebook.written_path} if written else None,
            drift_report=notebook.drift_report if written else None,
            delta=notebook.delta.to_dict() if notebook.delta else None,
        )
    if notebook.cells is None or notebook.drift_report is None:
        return ""
    delta_text = format_delta(notebook.delta, notebook.path) if notebook.delta else ""
    has_pins = bool(notebook.drift_report.manifest.dependencies)
    if written:
        report_text = core.format_console_drift_report(notebook.drift_report) if has_pins else ""
        return "\n\n".join(part for part in (delta_text, report_text) if part)
    parts = ([delta_text, ""] if delta_text else []) + [
        "--- [ STEP 1: PASTE INTO CELL 1 (MARKDOWN) ] ---\n",
        notebook.cells.markdown,
        "\n" + "=" * 80 + "\n",
        "--- [ STEP 2: PASTE INTO CELL 2 (CODE) ] ---\n",
        notebook.cells.code,
        "\n" + "=" * 80,
    ]
    if has_pins:
        parts += ["", core.format_console_drift_report(notebook.drift_report)]
    return "\n".join(parts)


def _log_diagnostics(report: models.NotebookAnalysisReport) -> None:
    """The warnings, notices, accelerator status and promotions found while analyzing."""
    gpu_info = report.gpu
    if report.warnings:
        logger.warning("⚠️ DIAGNOSTIC WARNINGS:")
        for warn in report.warnings:
            logger.warning(f"  • {warn.detail}")
        logger.warning("")

    if report.notices:
        for notice in report.notices:
            logger.info(notice.format_console())
        logger.info("")

    if gpu_info:
        if gpu_info.has_gpu:
            logger.info(f"⚡ Active accelerator detected: {gpu_info.device_name}\n")
        elif gpu_info.probe_errors:
            err_msg = "; ".join(gpu_info.probe_errors)
            logger.warning(f"⚠️ Accelerator detection encountered errors: {err_msg}\n")
        elif gpu_info.frameworks:
            fw_list = ", ".join(gpu_info.frameworks)
            logger.warning(f"⚠️ Acceleration Framework ({fw_list}) imported, but NO active accelerator detected in host runtime.\n")

    if report.promotions:
        for promo in report.promotions:
            logger.info(promo.detail)
        logger.info("")


def _write_mode(args: argparse.Namespace) -> Tuple[str, Optional[str]]:
    """The write mode and output directory the flags ask for. In-place wins over a directory, which
    wins over a companion file."""
    if args.in_place:
        return WriteMode.IN_PLACE, None
    if args.output_dir:
        return WriteMode.DIRECTORY, args.output_dir
    if args.output:
        return WriteMode.COMPANION, None
    return WriteMode.NONE, None


def _destination_description(args: argparse.Namespace) -> str:
    suffix = args.suffix if args.suffix is not None else ("" if args.output_dir else "_merged")
    if args.in_place:
        return "in-place"
    if args.output_dir:
        return f"directory: '{args.output_dir}'" + (f", suffix: '{suffix}'" if suffix else "")
    return f"suffix: '{suffix}'"


def _log_failures(result: Union[ScanResult, SnapshotResult]) -> None:
    """Names every notebook that could not be processed, and why."""
    failed = result.failed
    if not failed:
        return
    logger.warning(f"\n⚠️ {len(failed)} notebook(s) could not be processed and were skipped:")
    for notebook in failed:
        logger.warning(f"  • {core.relative_notebook_path(Path(notebook.path), result.target)}: {notebook.error}")


def _report_unreadable(notebook: Union[NotebookScan, NotebookSnapshot], is_json: bool) -> None:
    logger.error(f"❌ Error: {notebook.error}")
    if is_json:
        print(core.format_json_single_report(notebook.report))


def run_scan_file(args: argparse.Namespace, environment: Optional[Environment] = None) -> int:
    """One notebook file: runs scan (read-only, never contacts PyPI), prints the report and
    returns the exit code. Text formatting for a single file is not yet implemented."""
    if args.format == "text":
        logger.error("❌ Error: --format text is not yet supported for scanning a single file; use --format json.")
        return EXIT_FAILED
    target = args.target
    logger.info(f"🔍 Analyzing saved notebook file '{target}' via AST...")
    logger.info(f"📌 Active Python Interpreter: {sys.executable}\n")

    scan_result = endpoints.scan(target, environment=environment)
    scanned = scan_result.notebooks[0]
    if scanned.report.parse_error is not None:
        _report_unreadable(scanned, is_json=True)
    else:
        print(format_scan_result(scan_result))
    return scan_exit_code(scan_result)


def run_snapshot_file(args: argparse.Namespace, environment: Optional[Environment] = None) -> int:
    """One notebook file: runs snapshot, prints the result and returns the exit code."""
    target = args.target
    is_json = args.format == "json"
    logger.info(f"🔍 Analyzing saved notebook file '{target}' via AST...")
    logger.info(f"📌 Active Python Interpreter: {sys.executable}\n")

    write_mode, output_dir = _write_mode(args)
    options = SnapshotOptions(
        write_mode=write_mode, suffix=args.suffix, output_dir=output_dir,
        full_freeze=args.full_freeze, install_timeout=args.timeout,
    )
    snapshot_result = endpoints.snapshot(target, options, environment)
    snapped = snapshot_result.notebooks[0]
    if snapped.report.parse_error is not None:
        _report_unreadable(snapped, is_json)
        return snapshot_exit_code(snapshot_result)

    if not is_json:
        _log_diagnostics(snapped.report)
    if write_mode != WriteMode.NONE:
        logger.info(f"🚀 Writing updated notebook ({_destination_description(args)})...")
        if snapped.error is not None:
            logger.error(f"❌ Error: {snapped.error}")
            return snapshot_exit_code(snapshot_result)
        logger.info(f"✅ Updated '{snapped.written_path}'")

    out = format_snapshot_result(snapshot_result, args.format)
    if out:
        print(out)
    return snapshot_exit_code(snapshot_result)


# --- a directory of notebooks ---------------------------------------------------------------------

def run_scan_directory(args: argparse.Namespace, environment: Optional[Environment] = None) -> int:
    """A directory of notebooks: read-only scan. Prints the result and returns the exit code."""
    target = args.target
    if not os.path.isdir(target):
        logger.error(f"❌ Error: '{target}' is not a directory.")
        return EXIT_FAILED

    is_json = args.format == "json"
    scanned = endpoints.scan(target, ScanOptions(suffix=args.suffix), environment)
    assert scanned.batch_summary is not None
    deltas = {rel: d.to_dict() for rel, d in _directory_deltas(scanned).items()} or None
    if is_json:
        print(core.format_json_batch_report(scanned.batch_summary, deltas=deltas))
    else:
        print(core.format_console_report(scanned.batch_summary))
        if deltas:
            print("\n" + format_directory_deltas(scanned))
        _log_failures(scanned)
    return scan_exit_code(scanned)


def run_snapshot_directory(args: argparse.Namespace, environment: Optional[Environment] = None) -> int:
    """A directory of notebooks: runs snapshot across all of them. Whatever could be read is
    processed and (if a write flag was given) written. The notebooks that could not be are listed
    on stderr and in the report, and the exit code says the run was partial (1) or that nothing
    could be done (2).
    """
    target = args.target
    if not os.path.isdir(target):
        logger.error(f"❌ Error: '{target}' is not a directory.")
        return EXIT_FAILED

    is_json = args.format == "json"
    write_mode, output_dir = _write_mode(args)
    universal = args.universal or None

    options = SnapshotOptions(
        write_mode=write_mode, suffix=args.suffix, output_dir=output_dir, universal=universal,
        full_freeze=args.full_freeze, install_timeout=args.timeout,
    )
    result = endpoints.snapshot(target, options, environment)
    assert result.batch_summary is not None
    summary = result.batch_summary
    deltas = {rel: d.to_dict() for rel, d in _directory_deltas(result).items()} or None
    if not is_json:
        print(core.format_console_report(summary))
        if deltas:
            print("\n" + format_directory_deltas(result))

    artifacts_written: Dict[str, Any] = {}
    if result.universal_path is not None:
        artifacts_written["universal_manifest"] = result.universal_path
        logger.info(f"\n✅ Wrote universal repository manifest to '{result.universal_path}'")
    if result.error is not None:
        logger.error(f"❌ Error: {result.error}")

    if write_mode != WriteMode.NONE:
        logger.info(f"\n🚀 Writing per-notebook locked files ({_destination_description(args)})...")
        written = [n.written_path for n in result.notebooks if n.written_path is not None]
        for path in written:
            logger.info(f"  • Updated '{path}'")
        artifacts_written["locked_notebooks"] = written
        if written:
            logger.info("✅ Batch output complete.")
        if result.validation is not None and not is_json:
            print(core.format_console_batch_validation(result.validation))

    _log_failures(result)
    if is_json:
        print(core.format_json_batch_report(
            summary, artifacts_written=artifacts_written if artifacts_written else None, validation=result.validation,
            deltas=deltas,
        ))
    return snapshot_exit_code(result)


# --- the command line -------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="steady-py", description="Generate environment lockfiles for Jupyter Notebooks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--quiet", action="store_true", help="Suppress diagnostic and status logging outputs.")
    common.add_argument("--verbose", action="store_true", help="Enable verbose debug output.")

    scan_parser = subparsers.add_parser(
        "scan", parents=[common],
        help="Read-only: report a notebook's or directory's dependencies without writing anything.",
    )
    scan_parser.add_argument("target", help="Path to a .ipynb file or a directory.")
    scan_parser.add_argument(
        "--format", choices=["text", "json"], default="text",
        help="Output report format (default: 'text'). A single-file target currently supports 'json' only.",
    )
    scan_parser.add_argument("--suffix", default=None, help="Skip files already carrying this suffix when scanning a directory.")

    snapshot_parser = subparsers.add_parser(
        "snapshot", parents=[common],
        help="Analyze a notebook or directory and produce its setup cells and manifest.",
    )
    snapshot_parser.add_argument("target", help="Path to a .ipynb file or a directory.")
    snapshot_parser.add_argument("--format", choices=["text", "json"], default="text", help="Output report format (default: 'text').")
    snapshot_parser.add_argument("--full-freeze", action="store_true", help="Append full environment pip freeze after targeted manifest.")
    snapshot_parser.add_argument("--timeout", type=int, default=120, metavar="SECONDS", help="Per-package pip install timeout in seconds, baked into the generated notebook's install cell (default: 120).")
    snapshot_parser.add_argument(
        "--universal", nargs="?", const=constants.DEFAULT_UNIVERSAL_MANIFEST_NAME, default=None, metavar="FILENAME",
        help=f"Generate universal repository manifest (default: '{constants.DEFAULT_UNIVERSAL_MANIFEST_NAME}' when flag is provided). Directory targets only.",
    )
    snapshot_parser.add_argument("--output", action="store_true", help="Generate per-notebook merged lockfiles.")
    snapshot_parser.add_argument("--output-dir", metavar="DIR", help="Directory where generated locked notebooks should be written.")
    snapshot_parser.add_argument("--suffix", default=None, help="File suffix for merged notebook outputs (default: '_merged' alongside source, '' with --output-dir).")
    snapshot_parser.add_argument("--in-place", action="store_true", help="Overwrite original notebooks in-place instead of creating companion files.")

    check_parser = subparsers.add_parser(
        "check", parents=[common],
        help="Read-only: check an existing notebook's or directory's pinned manifest for drift against live PyPI.",
    )
    check_parser.add_argument("target", help="Path to a .ipynb file, a .py file, or a directory.")
    check_parser.add_argument("--format", choices=["text", "json"], default="text", help="Output report format (default: 'text').")
    check_parser.add_argument(
        "--root-dir", metavar="DIR",
        help="Root directory to re-verify root_dir-anchored local modules against; without it, those entries are reported as unverifiable, not silently skipped.",
    )

    return parser


def main() -> None:
    """The entry point: parses the flags, runs the verb they ask for, and exits with its code."""
    core.configure_console()
    localmodules.resolve_local_module.cache_clear()  # type: ignore[attr-defined]  # attached by _memoize_for_run
    core.build_manifest_entries.cache_clear()  # type: ignore[attr-defined]

    parser = build_parser()
    args = parser.parse_args()

    if args.quiet:
        logger.setLevel(logging.ERROR)
    elif args.verbose:
        logger.setLevel(logging.DEBUG)

    if args.command == "check":
        exit_code = run_check(args.target, output_format=args.format, root_dir=args.root_dir)
    elif args.command == "scan":
        exit_code = run_scan_directory(args) if os.path.isdir(args.target) else run_scan_file(args)
    else:  # "snapshot"
        exit_code = run_snapshot_directory(args) if os.path.isdir(args.target) else run_snapshot_file(args)

    sys.exit(exit_code)
