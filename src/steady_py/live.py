"""The live-session helper: snapshot of the running notebook, shown as the CLI shows a file's (D7)."""
from typing import Optional

from steady_py import cli, endpoints, reporting
from steady_py.constants import ATTENTION_DIAGNOSTICS, AUTHOR_BANNER
from steady_py.models import NotebookAnalysisReport
from steady_py.results import SnapshotOptions, SnapshotResult


def format_session(result: SnapshotResult) -> str:
    """The text a live snapshot shows: the author banner, what analysis found, the two cells to paste
    with their validation, and the declarations applied."""
    if result.skipped is not None:
        return result.skipped
    notebook = result.notebooks[0]
    parts = [AUTHOR_BANNER, ""]
    parts += [line for _, line in reporting.format_diagnostics(notebook.report)]
    if notebook.error is not None:
        return "\n".join([*parts, f"❌ Error: {notebook.error}"])
    parts.append(cli.format_snapshot_result(result))
    applied = reporting.format_declarations_applied(notebook.report.declarations_applied)
    if applied:
        parts += ["", applied]
    if notebook.cells is not None:
        parts += ["", _next_step(notebook.report)]
    return "\n".join(parts)


def _next_step(report: NotebookAnalysisReport) -> str:
    """What the author does next (install-lines spec, item 7): paste and delete this cell, or first
    settle what needs attention. A fix needs a fresh session, since snapshot reads every cell that ran."""
    open_items = sum(1 for w in report.warnings if w.type in ATTENTION_DIAGNOSTICS and w.accepted is None)
    if not open_items:
        return ("If this is your notebook: paste both cells at the top, run the notebook once to confirm, "
                "then delete this cell.")
    return (f"If this is your notebook: {open_items} warning(s) above need attention, and each offers an accept "
            "entry. Fix or accept them, then restart the session, run all, and run this cell again before pasting.")


def show_snapshot(options: Optional[SnapshotOptions] = None) -> None:
    """Runs snapshot on this notebook's session and prints its report and the two setup cells to
    paste. Returns nothing, so the cell shows only the text; steady_py.snapshot() returns the result."""
    print(format_session(endpoints.snapshot(None, options)))
