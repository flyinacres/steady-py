"""The command line: formats endpoint results and maps them to exit codes.

Formatting and exit-code mapping are pure functions of a result, so they are tested without a
process, a network or a notebook on disk.
"""
from __future__ import annotations

import sys
from typing import Optional, Tuple

from steady_py import core, endpoints
from steady_py.results import CheckOptions, CheckResult, NotebookCheck

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
