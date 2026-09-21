"""The command line: formats endpoint results and maps them to exit codes.

Formatting and exit-code mapping are pure functions of a result, so they are tested without a
process, a network or a notebook on disk.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from steady_py import core, endpoints
from steady_py.results import (
    CheckOptions, CheckResult, Delta, Environment, NotebookCheck, NotebookScan, NotebookSnapshot, ScanOptions,
    ScanResult, SnapshotOptions, SnapshotResult, WriteMode,
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


def run_single_file(args: argparse.Namespace, environment: Optional[Environment] = None) -> int:
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
        scan_result = endpoints.scan(target, environment=environment)
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

def run_directory(args: argparse.Namespace, environment: Optional[Environment] = None) -> int:
    """A directory of notebooks: scan when nothing is to be written, snapshot otherwise. Prints
    the result and returns the exit code.

    Whatever could be read is processed and written. The notebooks that could not be are listed
    on stderr and in the report, and the exit code says the run was partial (1) or that nothing
    could be done (2).
    """
    target = args.batch or args.notebook
    is_json = getattr(args, "format", "text") == "json"
    write_mode, output_dir = _write_mode(args)
    universal = args.universal or None
    wants_output = bool(universal) or write_mode != WriteMode.NONE

    if not os.path.isdir(target):
        logger.error(f"❌ Error: '{target}' is not a directory.")
        return EXIT_FAILED

    if not wants_output:
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
    parser.add_argument("notebook", nargs="?", help="Path to target .ipynb file or directory (when using --batch).")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output report format (default: 'text').")
    parser.add_argument("--full-freeze", action="store_true", help="Append full environment pip freeze after targeted manifest.")
    parser.add_argument("--timeout", type=int, default=120, metavar="SECONDS", help="Per-package pip install timeout in seconds, baked into the generated notebook's install cell (default: 120).")
    parser.add_argument("--quiet", action="store_true", help="Suppress diagnostic and status logging outputs.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug output.")
    parser.add_argument("--check-drift", action="store_true", help="Read-only: check an existing notebook's pinned manifest for drift against live PyPI, instead of generating a new one.")
    parser.add_argument("--root-dir", metavar="DIR", help="Root directory to re-verify root_dir-anchored local modules against during --check-drift; without it, those entries are reported as unverifiable, not silently skipped.")

    # Batch / Output Flags
    parser.add_argument("--batch", metavar="DIR", help="Run in batch mode across all notebooks in specified directory.")
    parser.add_argument("--analyze", action="store_true", help="Run batch analysis mode (default when --batch is provided).")
    parser.add_argument(
        "--universal",
        nargs="?",
        const=core.DEFAULT_UNIVERSAL_MANIFEST_NAME,
        default=None,
        metavar="FILENAME",
        help=f"Generate universal repository manifest (default: '{core.DEFAULT_UNIVERSAL_MANIFEST_NAME}' when flag is provided).",
    )
    parser.add_argument("--output", action="store_true", help="Generate per-notebook merged lockfiles.")
    parser.add_argument("--output-dir", metavar="DIR", help="Directory where generated locked notebooks should be written.")
    parser.add_argument("--suffix", default=None, help="File suffix for merged notebook outputs (default: '_merged' alongside source, '' with --output-dir).")
    parser.add_argument("--in-place", action="store_true", help="Overwrite original notebooks in-place instead of creating companion files.")
    return parser


def main() -> None:
    """The entry point: parses the flags, runs the verb they ask for, and exits with its code (in a
    live IPython session it returns instead, so the kernel is not killed)."""
    core.configure_console()
    core.resolve_local_module.cache_clear()  # type: ignore[attr-defined]  # attached by _memoize_for_run
    core.build_manifest_entries.cache_clear()  # type: ignore[attr-defined]

    parser = build_parser()
    args, _unknown = parser.parse_known_args()

    in_live_ipython = core.is_running_in_ipython()
    if in_live_ipython:
        core.sanitize_kernel_argv(args)

    if args.quiet:
        logger.setLevel(logging.ERROR)
    elif args.verbose:
        logger.setLevel(logging.DEBUG)

    if args.check_drift:
        if not args.notebook or not os.path.isfile(args.notebook):
            logger.error("❌ Error: --check-drift requires a target notebook or .py file path.")
            if in_live_ipython:
                return
            sys.exit(EXIT_FAILED)
        exit_code = run_check(args.notebook, output_format=args.format, root_dir=args.root_dir)
        if in_live_ipython:
            return
        sys.exit(exit_code)

    target_batch_dir = args.batch or (args.notebook if args.notebook and os.path.isdir(args.notebook) else None)

    if (args.output or args.in_place or args.output_dir) and not target_batch_dir and not (args.notebook and os.path.isfile(args.notebook)):
        logger.error("❌ Error: --output, --output-dir, or --in-place requires a target notebook file path or --batch directory.")
        if in_live_ipython:
            return
        sys.exit(EXIT_FAILED)

    if not args.notebook and not target_batch_dir and not in_live_ipython:
        parser.print_usage(sys.stderr)
        logger.error("❌ Error: a target notebook file, a directory, or --batch DIR is required.")
        sys.exit(EXIT_FAILED)

    exit_code = run_directory(args) if target_batch_dir else run_single_file(args)
    if exit_code and not in_live_ipython:
        sys.exit(exit_code)
