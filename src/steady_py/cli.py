"""The command line: formats endpoint results and maps them to exit codes.

Formatting and exit-code mapping are pure functions of a result, so they are tested without a
process, a network or a notebook on disk.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Tuple, Union

from steady_py import core, endpoints
from steady_py.results import (
    CheckOptions, CheckResult, NotebookCheck, NotebookScan, NotebookSnapshot, ScanResult, SnapshotOptions,
    SnapshotResult, WriteMode,
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
    that was not already known. Across several notebooks this is the worst code; how a
    directory aggregates is settled when check takes directories.
    """
    return max((_notebook_check_code(n) for n in result.notebooks), default=EXIT_OK)


def format_check_result(result: CheckResult, output_format: str = "text") -> Tuple[str, str]:
    """(stdout text, stderr text) for a check result. Either may be empty."""
    out_parts, err_parts = [], []
    for notebook in result.notebooks:
        if notebook.error is not None:
            err_parts.append(f"⚠️ {notebook.error}")
        elif notebook.report is None:
            out_parts.append(f"No STEADY_PY_MANIFEST found in {notebook.path} -- nothing to check.")
        elif output_format == "json":
            out_parts.append(core.format_json_drift_report(notebook.report))
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


# --- scan and snapshot, for one notebook file or the live session ------------------------------

def scan_exit_code(result: ScanResult) -> int:
    """0 when every notebook was read; 1 when one could not be (the rule for a notebook that
    cannot be processed is 2, and this changes with it)."""
    return EXIT_ATTENTION if result.failed else EXIT_OK


def snapshot_exit_code(result: SnapshotResult) -> int:
    """0 when every notebook was processed; 1 when one could not be (the rule for a notebook that
    cannot be processed is 2, and this changes with it)."""
    return EXIT_ATTENTION if result.failed else EXIT_OK


def format_scan_result(result: ScanResult) -> str:
    """The JSON report for a scanned notebook."""
    return core.format_json_single_report(result.notebooks[0].report)


def format_snapshot_result(result: SnapshotResult, output_format: str = "text") -> str:
    """What snapshot prints on stdout. Text is the two cells to paste when nothing was written,
    and the validation report otherwise; JSON is the analysis report, with the validation report
    and the written path when something was written."""
    notebook = result.notebooks[0]
    written = notebook.written_path is not None
    if output_format == "json":
        return core.format_json_single_report(
            notebook.report,
            artifacts_written={"locked_notebook": notebook.written_path} if written else None,
            drift_report=notebook.drift_report if written else None,
        )
    if notebook.cells is None or notebook.drift_report is None:
        return ""
    has_pins = bool(notebook.drift_report.manifest.dependencies)
    if written:
        return core.format_console_drift_report(notebook.drift_report) if has_pins else ""
    parts = [
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


def _log_diagnostics(report: core.NotebookAnalysisReport) -> None:
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


def _report_unreadable(notebook: Union[NotebookScan, NotebookSnapshot], is_json: bool) -> None:
    logger.error(f"❌ Error: {notebook.error}")
    if is_json:
        print(core.format_json_single_report(notebook.report))


def run_single_file(args: argparse.Namespace) -> int:
    """One notebook file, or the live IPython session: runs scan or snapshot as the flags ask,
    prints the result and returns the exit code.

    JSON without a write flag is a scan, which never contacts PyPI. Everything else is a snapshot.
    """
    in_live_ipython = core.is_running_in_ipython()
    is_json = getattr(args, "format", "text") == "json"
    target = args.notebook if (args.notebook and not os.path.isdir(args.notebook)) else None
    if target is None and not in_live_ipython:
        return EXIT_OK

    if target is not None:
        logger.info(f"🔍 [Path A] Analyzing saved notebook file '{target}' via AST...")
        logger.info(f"📌 Active Python Interpreter: {sys.executable}\n")
    else:
        logger.info("🔍 [Path B] Analyzing live IPython session kernel history via AST...")

    write_mode, output_dir = _write_mode(args)

    if is_json and write_mode == WriteMode.NONE:
        scan_result = endpoints.scan(target)
        scanned = scan_result.notebooks[0]
        if scanned.report.parse_error is not None:
            _report_unreadable(scanned, is_json)
        else:
            print(format_scan_result(scan_result))
        return scan_exit_code(scan_result)

    options = SnapshotOptions(
        write_mode=write_mode, suffix=args.suffix, output_dir=output_dir,
        full_freeze=args.full_freeze, install_timeout=args.timeout,
    )
    snapshot_result = endpoints.snapshot(target, options)
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
