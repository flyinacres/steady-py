"""Rendering results for people and tools: the console and JSON forms of the single-notebook, batch,
drift and batch-validation reports."""
import json
import sys
from typing import Any, Dict, List, Mapping, Optional, Tuple

from steady_py import analyze, drift
from steady_py.constants import BaselineStatus, ReportKind, SCHEMA_VERSION, TOOL_VERSION
from steady_py.models import BatchAnalysisSummary, DriftFinding, GpuInfo, NotebookAnalysisReport


def format_console_drift_report(report: drift.DriftCheckReport) -> str:
    """Formats a drift.DriftCheckReport into a human-readable stdout report string."""
    out = []
    out.append("=" * 80)
    if report.kind == ReportKind.VALIDATION:
        out.append("INITIAL DEPENDENCY VALIDATION")
    else:
        out.append("DEPENDENCY DRIFT CHECK")
        out.append(f"Target: {report.target}")
    out.append(f"Checked: {report.checked_at}")
    if report.kind == ReportKind.CHECK:
        if report.baseline_recorded:
            counts = report.baseline_counts()
            unclear = counts["new"] + counts["not_checked_at_generation"]
            out.append(f"Since generation: {unclear} new, {counts['known']} already known.")
        else:
            out.append("Note: no generation-time baseline in this manifest, so findings are not classified as new or known.")
    out.append("=" * 80 + "\n")

    def _line(f: DriftFinding) -> str:
        tag = {
            BaselineStatus.NEW: "[new] ",
            BaselineStatus.KNOWN: "[known] ",
            BaselineStatus.NOT_CHECKED_AT_GENERATION: "[not checked at generation] ",
        }.get(f.baseline_status or "", "")
        return f"  • {tag}[{f.signal}] {f.message}"

    if report.confirmed:
        out.append(f"🔴 CONFIRMED ISSUES ({len(report.confirmed)}):")
        out.extend(_line(f) for f in report.confirmed)
        out.append("")

    if report.heuristic:
        out.append(f"🟡 WORTH REVIEWING -- heuristic, not confirmed ({len(report.heuristic)}):")
        out.extend(_line(f) for f in report.heuristic)
        out.append("")

    if report.notices:
        out.append(f"ℹ️ CUSTOM SOURCES -- already known at generation, not counted as drift ({len(report.notices)}):")
        out.extend(_line(f) for f in report.notices)
        out.append("")

    if report.errors:
        out.append(f"⚠️ COULD NOT CHECK ({len(report.errors)}):")
        for f in report.errors:
            out.append(f"  • {f.message}")
        out.append("")

    out.append("-" * 80)
    if report.is_clean:
        if report.kind == ReportKind.VALIDATION:
            out.append("STATUS: ✅ Clean. No issues found in these pins.")
        else:
            noted = f" ({len(report.notices)} custom-source package(s) noted above)." if report.notices else ""
            out.append("STATUS: ✅ Clean. No drift detected against the pinned manifest." + noted)
    elif not (report.has_confirmed or report.has_errors or report.has_actionable_heuristic):
        out.append(f"STATUS: ✅ No new issues since generation ({len(report.heuristic)} known item(s) still present).")
    else:
        parts = []
        if report.has_confirmed:
            parts.append(f"{len(report.confirmed)} confirmed issue(s)")
        if report.has_errors:
            parts.append(f"{len(report.errors)} pin(s) could not be checked")
        if report.has_heuristic:
            parts.append(f"{len(report.heuristic)} item(s) worth reviewing")
        out.append(f"STATUS: ⚠️ {'; '.join(parts)}")
    out.append("=" * 80)

    return "\n".join(out)


def format_json_drift_report(report: drift.DriftCheckReport) -> str:
    """Formats a drift.DriftCheckReport into valid machine-readable JSON."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "mode": "initial_validation" if report.kind == ReportKind.VALIDATION else "check_drift",
        **report.to_dict(),
    }
    return json.dumps(payload, indent=2)


def format_console_batch_validation(validation: drift.BatchValidation, max_names: int = 5) -> str:
    """Human-readable aggregate section for the end of a batch run."""
    n = validation.notebooks_checked
    if not validation.findings:
        return f"✅ Batch validation: {n} notebook(s) checked, no issues found in their pins."

    out = ["=" * 80, "BATCH DEPENDENCY VALIDATION", f"Checked {n} notebook(s).", "=" * 80 + "\n"]
    sections = [
        ("confirmed", "🔴 CONFIRMED ISSUES"),
        ("heuristic", "🟡 WORTH REVIEWING -- heuristic, not confirmed"),
        ("error", "⚠️ COULD NOT CHECK"),
    ]
    for severity, title in sections:
        groups = [g for g in validation.findings if g.severity == severity]
        if not groups:
            continue
        out.append(f"{title} ({len(groups)}):")
        for g in groups:
            out.append(f"  • [{g.signal}] {g.message}")
            shown = g.notebooks[:max_names]
            extra = len(g.notebooks) - len(shown)
            names = ", ".join(shown) + (f" (+{extra} more)" if extra else "")
            out.append(f"      affects {len(g.notebooks)} notebook(s): {names}")
        out.append("")

    affected = [nb for nb in validation.notebooks if nb.has_findings]
    out.append("NOTEBOOKS WITH FINDINGS:")
    for nb in affected:
        out.append(
            f"  {nb.path}: {nb.confirmed} confirmed, {nb.heuristic} worth reviewing, "
            f"{nb.errors} could not be checked"
        )
    totals = validation.totals()
    parts = []
    if totals["confirmed"]:
        parts.append(f"{totals['confirmed']} distinct confirmed issue(s)")
    if totals["heuristic"]:
        parts.append(f"{totals['heuristic']} distinct item(s) worth reviewing")
    if totals["errors"]:
        parts.append(f"{totals['errors']} could not be checked")
    out.extend(["", "-" * 80, f"STATUS: ⚠️ {'; '.join(parts)} -- in {len(affected)} of {n} notebook(s)", "=" * 80])
    return "\n".join(out)


def format_console_report(summary: BatchAnalysisSummary) -> str:
    """Formats a BatchAnalysisSummary into a human-readable stdout report string."""
    out = []
    out.append("=" * 80)
    out.append("REPOSITORY REPRODUCIBILITY SUMMARY")
    out.append(f"Target Directory: {summary.target_dir}")
    out.append(f"Active Interpreter: {sys.executable}")
    out.append("=" * 80 + "\n")

    out.append("📁 NOTEBOOK INVENTORY & LANGUAGE SCAN:")
    out.append(f"  • Python (.ipynb): {summary.total_python_notebooks} files analyzed")
    
    if summary.companion_skipped_count > 0:
        out.append(f"  • Companion outputs skipped: {summary.companion_skipped_count} files")

    if summary.non_python_count > 0:
        lang_str = ", ".join([f"{k} ({v})" for k, v in summary.non_python_languages.items()])
        out.append(f"  • Non-Python skipped: {summary.non_python_count} files [{lang_str}]")
    else:
        out.append("  • Non-Python skipped: 0 files")

    err_count = len(summary.parse_errors)
    out.append(f"  • File / Parse Errors: {err_count} files")
    out.append("")

    if err_count > 0:
        out.append("❌ FILE & PARSE ERRORS:")
        for err_dict in summary.parse_errors:
            out.append(f"  • {err_dict['path']}")
            out.append(f"    └─ Cause: {err_dict['cause']}")
        out.append("")

    out.append(f"📦 REPOSITORY PACKAGE SUMMARY (Across {summary.total_python_notebooks} Python notebooks):")
    matched_list = sorted(summary.matched_packages)
    out.append(f"  • Installed & Verified: {len(matched_list)} packages ({', '.join(matched_list[:5])}{'...' if len(matched_list) > 5 else ''})")
    
    if summary.missing_packages:
        out.append(f"  • Packages not resolvable via pip-freeze or local file scan: {len(summary.missing_packages)}")
        out.append("    (Not found installed, nor as a sibling file/package next to the notebook or in the declared")
        out.append("     root dir. If any of these resolve via a custom sys.path setup -- PYTHONPATH, an IDE project")
        out.append("     root, an editable install, or a platform like Databricks Repos -- this is a false positive;")
        out.append("     verify by running the notebook directly before assuming a real gap. Otherwise, run")
        out.append("     'pip install <package>'.)")
        for pkg, nbs in sorted(summary.missing_packages.items()):
            nb_list = ", ".join(sorted(set(nbs))[:3])
            more = f", +{len(set(nbs))-3} more" if len(set(nbs)) > 3 else ""
            out.append(f"      - {pkg} (imported in: {nb_list}{more})")
    else:
        out.append("  • Packages not resolvable via pip-freeze or local file scan: 0")

    if summary.guarded_packages:
        out.append(f"  • Guarded/optional imports (inside try/except): {len(summary.guarded_packages)}")
        for pkg, nbs in sorted(summary.guarded_packages.items()):
            nb_list = ", ".join(sorted(set(nbs))[:3])
            more = f", +{len(set(nbs))-3} more" if len(set(nbs)) > 3 else ""
            out.append(f"      - {pkg} (imported in: {nb_list}{more})")
    out.append("")

    if summary.dynamic_warnings or summary.magic_warnings:
        out.append("⚠️ NOTICES & WARNINGS:")
        for warn in summary.dynamic_warnings:
            out.append(f"  • {warn.format_console()}")
        for warn in summary.magic_warnings:
            out.append(f"  • {warn.format_console()}")
        out.append("")

    if summary.magic_notices:
        out.append("ℹ️ SYSTEM & CONDA COMMANDS:")
        for notice in summary.magic_notices:
            out.append(f"  • {notice.format_console()}")
        out.append("")

    if summary.promotions:
        out.append("💡 AUTOMATIC EXTRA PROMOTIONS:")
        for promo in summary.promotions:
            out.append(f"  • {promo.detail}")
        out.append("")

    out.append("⚡ ACCELERATOR & DOWNLOAD INDEX CHECK:")
    if summary.batch_hw_cache and summary.batch_hw_cache.has_gpu:
        out.append(f"  • Active Hardware Accelerator: {summary.batch_hw_cache.device_name}")
    elif summary.batch_hw_cache and summary.batch_hw_cache.probe_errors:
        err_msg = "; ".join(summary.batch_hw_cache.probe_errors)
        out.append(f"  • Active Hardware Accelerator: None detected (⚠️ Detection encountered errors: {err_msg})")
    else:
        out.append("  • Active Hardware Accelerator: None (CPU-only execution environment)")

    if summary.primary_url:
        out.append(f"  • Primary Index URL: {summary.primary_url}")
        out.append(f"    └─ Selection Rule: {summary.primary_url_reason}")
    else:
        out.append("  • Extra Index URLs Harvested: None")

    if summary.batch_hardware_warnings:
        out.append("  • Custom Build Tag Warnings:")
        for pkg, nbs in sorted(summary.batch_hardware_warnings.items()):
            nb_list = ", ".join(sorted(set(nbs))[:3])
            more = f", +{len(set(nbs))-3} more" if len(set(nbs)) > 3 else ""
            out.append(f"      ⚠️ {pkg} (in: {nb_list}{more}) — No download URL harvested in code cells.")

    out.append("\n" + "-" * 80)
    if err_count > 0:
        out.append("STATUS: ⚠️ ATTENTION REQUIRED - Resolve file/parse errors above before building manifests.")
    else:
        out.append(f"STATUS: Ready. All {summary.total_python_notebooks} Python notebooks parsed successfully.")
    out.append("=" * 80)

    return "\n".join(out)


def format_json_batch_report(
    summary: BatchAnalysisSummary,
    artifacts_written: Optional[Dict[str, Any]] = None,
    validation: Optional["drift.BatchValidation"] = None,
    deltas: Optional[Dict[str, Any]] = None,
) -> str:
    """Formats a BatchAnalysisSummary into valid machine-readable JSON."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "mode": "batch",
        "target_dir": summary.target_dir,
        "environment": {
            "active_interpreter": sys.executable,
            "python_version": [sys.version_info.major, sys.version_info.minor, sys.version_info.micro]
        },
        "summary": {
            "is_clean": summary.is_clean,
            "total_python_notebooks": summary.total_python_notebooks,
            "non_python_count": summary.non_python_count,
            "non_python_languages": summary.non_python_languages,
            "companion_skipped_count": summary.companion_skipped_count,
            "parse_errors": summary.parse_errors,
            "matched_packages": sorted(list(summary.matched_packages)),
            "missing_packages": summary.missing_packages,
            "guarded_packages": summary.guarded_packages,
            "hardware_warnings": summary.batch_hardware_warnings,
            "promotions": [p.to_dict() for p in summary.promotions],
            "primary_index_url": summary.primary_url,
            "primary_index_url_reason": summary.primary_url_reason
        },
        "notebooks": [nb.to_dict() for nb in summary.notebooks],
        "artifacts_written": artifacts_written,
        "validation": validation.to_dict() if validation else None,
        "deltas": deltas,
    }
    return json.dumps(payload, indent=2)


def format_json_single_report(
    nb_report: NotebookAnalysisReport,
    artifacts_written: Optional[Dict[str, Any]] = None,
    drift_report: Optional["drift.DriftCheckReport"] = None,
    delta: Optional[Dict[str, Any]] = None,
) -> str:
    """Formats a single NotebookAnalysisReport into valid machine-readable JSON."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "mode": "single_file",
        "environment": {
            "active_interpreter": sys.executable,
            "python_version": [sys.version_info.major, sys.version_info.minor, sys.version_info.micro]
        },
        **nb_report.to_dict(),
        "artifacts_written": artifacts_written,
        "drift_check": drift_report.to_dict() if drift_report else None,
        "delta": delta,
    }
    return json.dumps(payload, indent=2)


def generate_batch_analysis_report(
    repo_map: analyze.RepoEnvironmentMap, 
    frozen_env: Dict[str, str], 
    pkg_dist_map: Mapping[str, List[str]], 
    batch_hw_cache: Optional[GpuInfo]
) -> Tuple[str, bool]:
    """Orchestrates batch repository analysis and returns (report_text, is_clean)."""
    summary = analyze.analyze_batch_repository(repo_map, frozen_env, pkg_dist_map, batch_hw_cache)
    report_text = format_console_report(summary)
    return report_text, summary.is_clean
