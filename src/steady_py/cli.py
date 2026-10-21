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
from typing import Any, Dict, List, Optional, Tuple

from steady_py import constants, declarations, endpoints, models, reporting, util
from steady_py.constants import ATTENTION_DIAGNOSTICS
from steady_py.results import (
    CheckOptions, CheckResult, Delta, Environment, NotebookCheck, NotebookSnapshot, SnapshotOptions, SnapshotResult,
    TargetKind, WriteMode,
)

logger = logging.getLogger("steady_py")


def configure_console() -> None:
    """CLI-only process setup: UTF-8 stdout/stderr (Windows and redirected output) and a plain
    stderr log handler at INFO, which replaces propagation to the root logger. Called once from
    main(), never at import."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    for placeholder in [h for h in logger.handlers if isinstance(h, logging.NullHandler)]:
        logger.removeHandler(placeholder)  # the real handler replaces it, leaving exactly one
    if not any(type(h) is logging.StreamHandler for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.propagate = False  # a host's root handler would print every message a second time

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
        return reporting.format_json_drift_report(notebook.report)
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
                {"path": util.relative_notebook_path(Path(n.path), result.target), "manifest_found": n.report is not None,
                 "error": n.error, "drift_check": n.report.to_dict() if n.report is not None else None}
                for n in notebooks
            ],
            "validation": result.validation.to_dict() if result.validation else None,
        }
        errors = [f"⚠️ {util.relative_notebook_path(Path(n.path), result.target)}: {n.error}" for n in unreadable]
        return json.dumps(payload, indent=2), "\n".join(errors)
    if not notebooks:
        return f"No notebooks found in {result.target} -- nothing to check.", ""
    out = [f"Checked {len(notebooks)} notebook(s) in {result.target}: {len(with_manifest)} with a manifest, "
           f"{without} without (nothing to check), {len(unreadable)} could not be read."]
    if result.validation is not None:
        out += ["", reporting.format_console_batch_validation(result.validation)]
    errors = [f"⚠️ {util.relative_notebook_path(Path(n.path), result.target)}: {n.error}" for n in unreadable]
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
            out_parts.append(reporting.format_console_drift_report(notebook.report))
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


# --- snapshot, for one notebook file or a directory ------------------------------------------

def snapshot_exit_code(result: SnapshotResult) -> int:
    """0 when everything was processed; 1 when some of it was and some was not (an unreadable
    notebook, a failed write, a failure of the run as a whole such as the universal file), or when
    a notebook needs a dependency no runner can install or steady-py can't pin
    (ATTENTION_DIAGNOSTICS, ED3); 2 when nothing could be processed. One unreadable file is 2."""
    failed = len(result.failed)
    processed = len(result.notebooks) - failed
    if failed or result.error is not None:
        return EXIT_ATTENTION if processed else EXIT_FAILED
    attention = any(w.type in ATTENTION_DIAGNOSTICS and w.accepted is None
                    for n in result.notebooks if n.report for w in n.report.warnings)
    return EXIT_ATTENTION if attention else EXIT_OK


def _gpu_label(setting: Optional[Dict[str, Any]]) -> str:
    if not setting:
        return "none"
    return str(setting.get("device_name") or "GPU") if setting.get("has_gpu") else "no GPU"


def format_delta(delta: Delta, label: str) -> str:
    """What a snapshot would change (or did change) in the manifest a notebook already carried."""
    if not delta.has_changes:
        return f"No changes since the existing manifest in {label}."
    lines = [f"Changes since the existing manifest in {label}:"]
    lines += [f"  + {c.name}{f'=={c.new_version}' if c.new_version else ''} (added)" for c in delta.added]
    lines += [f"  - {c.name}{f'=={c.old_version}' if c.old_version else ''} (removed)" for c in delta.removed]
    lines += [f"  ~ {c.name} {c.old_version} -> {c.new_version}" for c in delta.version_changes]
    if delta.python_version:
        lines.append(f"  Python {delta.python_version[0]} -> {delta.python_version[1]}")
    if delta.gpu:
        lines.append(f"  Accelerator: {_gpu_label(delta.gpu[0])} -> {_gpu_label(delta.gpu[1])}")
    if delta.findings_appeared:
        lines.append("  New findings: " + ", ".join(":".join(key) for key in delta.findings_appeared))
    if delta.findings_resolved:
        lines.append("  Resolved findings: " + ", ".join(":".join(key) for key in delta.findings_resolved))
    for change in delta.field_changes:
        where = f"{change.name} " if change.name else ""
        if change.field == "entry":
            sign = "+" if change.before is None else "-"
            lines.append(f"  {sign} {change.section.replace('_', ' ')[:-1]} {change.name}")
        else:
            lines.append(f"  * {where}{change.field}: {_value(change.before)} -> {_value(change.after)}")
    if delta.unchecked_appeared:
        lines.append("  Could not be checked: " + ", ".join(p or "(dependency graph)" for p in delta.unchecked_appeared))
    if delta.unchecked_resolved:
        lines.append("  Checked again: " + ", ".join(p or "(dependency graph)" for p in delta.unchecked_resolved))
    if delta.version_changes:
        lines.append("  Versions come from the environment this ran in, so a different machine can show version changes the code did not cause.")
    return "\n".join(lines)


def _value(value: Any) -> str:
    """A changed field's value, compactly."""
    if value is None or value == [] or value == {}:
        return "none"
    if isinstance(value, list) and all(isinstance(v, list) for v in value):
        return " ".join(" ".join(str(p) for p in pair if p is not None) for pair in value)  # options
    return json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)


def _directory_deltas(result: SnapshotResult) -> Dict[str, Delta]:
    """The delta of each notebook that already had a manifest, keyed by path relative to the target."""
    return {
        util.relative_notebook_path(Path(n.path), result.target): n.delta
        for n in result.notebooks if n.delta is not None
    }


def format_directory_deltas(result: SnapshotResult) -> str:
    return "\n\n".join(format_delta(delta, rel) for rel, delta in _directory_deltas(result).items())


def format_snapshot_result(result: SnapshotResult, output_format: str = "text") -> str:
    """What snapshot prints on stdout. Text is the two cells to paste when nothing was written,
    and the validation report otherwise, each after the delta when the notebook already had a
    manifest; JSON is the analysis report, with the delta, and with the validation report and
    the written path when something was written."""
    notebook = result.notebooks[0]
    written = notebook.written_path is not None
    if output_format == "json":
        return reporting.format_json_single_report(
            notebook.report,
            artifacts_written={"locked_notebook": notebook.written_path} if written else None,
            drift_report=notebook.drift_report if written else None,
            delta=notebook.delta.to_dict() if notebook.delta else None,
        )
    if notebook.cells is None or notebook.drift_report is None:
        return ""
    delta_text = format_delta(notebook.delta, notebook.path) if notebook.delta else ""
    has_pins = bool(notebook.drift_report.manifest.pins())
    if written:
        report_text = reporting.format_console_drift_report(notebook.drift_report) if has_pins else ""
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
        parts += ["", reporting.format_console_drift_report(notebook.drift_report)]
    return "\n".join(parts)


def _log_diagnostics(report: models.NotebookAnalysisReport) -> None:
    """The warnings, notices, install-line advice, accelerator status and promotions found while analyzing."""
    for is_warning, line in reporting.format_diagnostics(report):
        (logger.warning if is_warning else logger.info)(line)


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


def _log_failures(result: SnapshotResult) -> None:
    """Names every notebook that could not be processed, and why."""
    failed = result.failed
    if not failed:
        return
    logger.warning(f"\n⚠️ {len(failed)} notebook(s) could not be processed and were skipped:")
    for notebook in failed:
        logger.warning(f"  • {util.relative_notebook_path(Path(notebook.path), result.target)}: {notebook.error}")


def _report_unreadable(notebook: NotebookSnapshot, is_json: bool) -> None:
    logger.error(f"❌ Error: {notebook.error}")
    if is_json:
        print(reporting.format_json_single_report(notebook.report))


def run_snapshot_file(args: argparse.Namespace, environment: Optional[Environment] = None) -> int:
    """One notebook file: runs snapshot, prints the result and returns the exit code."""
    target = args.target
    is_json = args.format == "json"
    if not is_json:
        logger.info(constants.AUTHOR_BANNER + "\n")
    logger.info(f"🔍 Analyzing saved notebook file '{target}' via AST...")
    logger.info(f"📌 Active Python Interpreter: {sys.executable}\n")

    write_mode, output_dir = _write_mode(args)
    options = SnapshotOptions(
        write_mode=write_mode, suffix=args.suffix, output_dir=output_dir,
        full_freeze=args.full_freeze, install_timeout=args.timeout, declarations_file=getattr(args, "declarations", None),
    )
    snapshot_result = endpoints.snapshot(target, options, environment)
    snapped = snapshot_result.notebooks[0]
    if snapped.report.parse_error is not None:
        _report_unreadable(snapped, is_json)
        return snapshot_exit_code(snapshot_result)

    if not is_json:
        _log_diagnostics(snapped.report)
    if snapped.error is not None:  # a failed write, or an existing manifest it must not replace
        logger.error(f"❌ Error: {snapped.error}")
        if is_json:
            print(format_snapshot_result(snapshot_result, args.format))
        return snapshot_exit_code(snapshot_result)
    if write_mode != WriteMode.NONE:
        logger.info(f"🚀 Writing updated notebook ({_destination_description(args)})...")
        if snapped.unchanged:
            logger.info(f"✅ Nothing changed; '{snapped.path}' was left as it was.")
        else:
            logger.info(f"✅ Updated '{snapped.written_path}'")

    out = format_snapshot_result(snapshot_result, args.format)
    if out:
        print(out)
    applied = reporting.format_declarations_applied(snapped.report.declarations_applied)
    if applied and not is_json:
        print("\n" + applied)
    return snapshot_exit_code(snapshot_result)


# --- a directory of notebooks ---------------------------------------------------------------------

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
    if not is_json:
        logger.info(constants.AUTHOR_BANNER + "\n")
    write_mode, output_dir = _write_mode(args)
    universal = args.universal or None

    options = SnapshotOptions(
        write_mode=write_mode, suffix=args.suffix, output_dir=output_dir, universal=universal,
        full_freeze=args.full_freeze, install_timeout=args.timeout, declarations_file=getattr(args, "declarations", None),
    )
    result = endpoints.snapshot(target, options, environment)
    assert result.batch_summary is not None
    summary = result.batch_summary
    deltas = {rel: d.to_dict() for rel, d in _directory_deltas(result).items()} or None
    if not is_json:
        print(reporting.format_console_report(summary))
        if deltas:
            print("\n" + format_directory_deltas(result))
        for notebook in result.notebooks:
            applied = reporting.format_declarations_applied(notebook.report.declarations_applied)
            if applied:
                print(f"\n{util.relative_notebook_path(Path(notebook.path), target)}\n{applied}")

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
        unchanged = sum(1 for n in result.notebooks if n.unchanged)
        if unchanged:
            logger.info(f"  • {unchanged} notebook(s) had nothing to change and were left as they were.")
        artifacts_written["locked_notebooks"] = written
        if written:
            logger.info("✅ Batch output complete.")
        if result.validation is not None and not is_json:
            print(reporting.format_console_batch_validation(result.validation))

    _log_failures(result)
    if is_json:
        print(reporting.format_json_batch_report(
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
    snapshot_parser.add_argument(
        "--declarations", metavar="FILE", default=None,
        help=f"A file holding {constants.DECLARATIONS_NAME} = [...], applied to every notebook before its own declarations cell.",
    )

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


def main(argv: Optional[List[str]] = None) -> None:
    """The entry point: parses the flags (`argv`, default sys.argv), runs the verb they ask for, and exits with its code."""
    configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.quiet:
        logger.setLevel(logging.ERROR)
    elif args.verbose:
        logger.setLevel(logging.DEBUG)

    if args.command == "check":
        exit_code = run_check(args.target, output_format=args.format, root_dir=args.root_dir)
    else:  # "snapshot"
        try:
            exit_code = run_snapshot_directory(args) if os.path.isdir(args.target) else run_snapshot_file(args)
        except declarations.DeclarationError as e:  # the declarations file: nothing ran
            logger.error(f"❌ Error: {e}")
            exit_code = EXIT_FAILED

    sys.exit(exit_code)
