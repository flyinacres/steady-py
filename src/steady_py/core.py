#!/usr/bin/env bash
#!/usr/bin/env python3
"""
steady-py (v44)
Headless Jupyter Notebook Dependency Scanner & Lockfile Generator.

Standalone utility (requires `packaging` and `resolvelib`) for analyzing notebook environments,
detecting GPU/accelerator requirements, harvesting scoped index URLs, and emitting
reproducible lockfile manifests and isolated sequential installation blueprints.

=====================================================================
🚀 QUICKSTART FOR JUPYTER / COLAB / DATABRICKS USERS
=====================================================================
If you are running inside a Jupyter notebook cell:
  1. Paste this entire file into a notebook cell.
  2. Run:
       import steady_py.cli as spy
       spy.main()
  3. Copy the output setup cells into the top of your notebook.

For full CLI documentation, batch directory workflows, and detailed instructions, 
see the repository README:
👉 https://github.com/flyinacres/notebook_env/blob/main/README.md

Execution Modes:
  1. Single Notebook CLI:  python -m steady_py notebook.ipynb [--format {text,json}] [--output | --output-dir DIR | --in-place]
  2. Batch Repo Directory: python -m steady_py --batch ./repo [--format {text,json}] [--universal [FILENAME]] [--output | --output-dir DIR | --in-place]
  3. Live IPython Kernel:   import steady_py.cli as spy; spy.main()
"""

# =====================================================================
# IMPORTS & LOGGING
# =====================================================================

import json
import sys
import logging
import subprocess
import tempfile
import importlib.metadata
import importlib.util
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Any, Mapping

from steady_py.constants import (
    BaselineStatus,
    HELP_URL,
    ReportKind,
    SCHEMA_VERSION,
    TOOL_VERSION,
)
from steady_py.models import (
    BatchAnalysisSummary,
    DriftFinding,
    GpuInfo,
    NotebookAnalysisReport,
)
from steady_py import analyze, drift


# Diagnostics go through this logger. Importing the module must not touch process-global state
# (the standard streams, other loggers' handlers), so the logger itself is only given a NullHandler;
# the CLI entry point (cli.main) calls configure_console() to attach the stderr handler and force UTF-8
# output. It never propagates to the root logger, so a host that configures logging (an IPython
# session, a test runner) does not print every message twice. The name is explicit, not __name__,
# because running the file as a script would otherwise name it "__main__".
logger = logging.getLogger("steady_py")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:  # guarded: the file is re-executed when pasted into a live kernel more than once
    logger.addHandler(logging.NullHandler())


def configure_console() -> None:
    """CLI-only process setup: UTF-8 stdout/stderr (Windows and redirected output) and a plain
    stderr log handler at INFO. Called once from main(), never at import."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    for placeholder in [h for h in logger.handlers if isinstance(h, logging.NullHandler)]:
        logger.removeHandler(placeholder)  # the real handler replaces it, leaving exactly one
    if not any(type(h) is logging.StreamHandler for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)


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


# =====================================================================
# BLUEPRINT & SEQUENTIAL INSTALL SETUP GENERATOR
# =====================================================================


# =====================================================================
# BATCH ORCHESTRATION & CLI DISPATCH
# =====================================================================


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


@dataclass
class InstallResult:
    """What steady_py.install() actually did: how many of the manifest's packages installed
    successfully and which failed, so a caller can check success without scraping printed
    output. `failed` holds each failed specifier, e.g. "broken_pkg==1.0.0"."""
    total: int
    installed: int
    failed: List[str]

    @property
    def ok(self) -> bool:
        return not self.failed


def install(manifest: Dict[str, Any], timeout: int = 120) -> InstallResult:
    """The runtime installer: the `install` verb, and the only endpoint that runs at
    notebook run time. Reads a STEADY_PY_MANIFEST dict and installs each pinned
    dependency sequentially via pip (to avoid index conflicts), printing progress and a
    final summary. Called from generated Cell 2 as `steady_py.install(STEADY_PY_MANIFEST)`.
    Needs internet; internet-off runs are unsupported.
    """
    py = manifest.get("python_version") or {}
    required = (py.get("major"), py.get("minor"))
    current = (sys.version_info.major, sys.version_info.minor)
    if None not in required and current != required:
        req_ver = f"{required[0]}.{required[1]}"
        curr_ver = f"{current[0]}.{current[1]}"
        print(f"⚠️ This code was created with Python {req_ver}. You are trying to run it with {curr_ver}.")
        print(f"If installation fails, consider changing your runtime Python version back to {req_ver}.\n")

    print(f"Applying verified environment dependencies [{manifest.get('generated_at', '')}]...")
    print("💡 Note: Dependencies are installed sequentially to prevent index conflicts.\n")

    passed_count = 0
    failed_packages: List[Tuple[str, str, List[str], str]] = []
    any_install_performed = False
    dependencies = manifest.get("dependencies", [])
    raw_installs = manifest.get("raw_installs", [])
    total_deps = len(dependencies) + len(raw_installs)
    installed_baseline: Dict[str, str] = {}

    def _run_pip_subprocess(cmd: List[str], to: int) -> Tuple[int, List[str]]:
        captured: List[str] = []
        returncode = 0
        try:
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as tmp_out:
                proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=tmp_out, stderr=subprocess.STDOUT, timeout=to)
                returncode = proc.returncode
                tmp_out.seek(0)
                for line in tmp_out.read().splitlines():
                    if line.strip():
                        captured.append(line)
                        print(f"    {line}")
                sys.stdout.flush()
        except subprocess.TimeoutExpired:
            returncode = -1
            captured.append(f"Error: installation exceeded per-package timeout limit ({to}s).")
            print(f"    ❌ Installation timed out after {to}s.")
        except Exception as exc:
            returncode = -1
            captured.append(f"Execution failed: {exc}")
            print(f"    ❌ Execution failed: {exc}")
        return returncode, captured

    idx = 0
    for item in dependencies:
        idx += 1
        name = item["name"]
        ver = item.get("version", "")
        flags = item.get("flags", [])
        specifier = f"{name}=={ver}" if ver else name

        already_satisfied = False
        try:
            current_ver = importlib.metadata.version(name)
            if not ver or current_ver == ver:
                already_satisfied = True
                passed_count += 1
                installed_baseline[name] = current_ver
                print(f"[{idx}/{total_deps}] ⚡ {name} ({current_ver}) already satisfied in environment")
        except Exception:
            pass

        if already_satisfied:
            continue

        cmd = [
            sys.executable, "-m", "pip", "install",
            "--no-input", "--disable-pip-version-check", "--no-warn-script-location",
            specifier,
        ] + flags

        print(f"[{idx}/{total_deps}] 📦 Installing {specifier}...")
        sys.stdout.flush()

        returncode, captured_output = _run_pip_subprocess(cmd, timeout)

        if returncode == 0:
            passed_count += 1
            any_install_performed = True
            print(f"    ✅ {specifier} installed successfully")
            try:
                current_ver = importlib.metadata.version(name)
                installed_baseline[name] = current_ver
            except Exception:
                pass
            for prev_pkg, prev_ver in list(installed_baseline.items()):
                if prev_pkg == name:
                    continue
                try:
                    active_now = importlib.metadata.version(prev_pkg)
                    if active_now != prev_ver:
                        print(f"   ⚠️ Dependency Drift: Installing '{specifier}' caused '{prev_pkg}' to drift from {prev_ver} ➔ {active_now}")
                        installed_baseline[prev_pkg] = active_now
                except Exception:
                    pass
        else:
            err_snippet = captured_output[-1] if captured_output else "Unknown pip error"
            failed_packages.append((specifier, ver, flags, "\n".join(captured_output)))
            print(f"    ❌ {specifier} failed to install (exit code {returncode})")
            if name in manifest.get("custom_sourced", []):
                print("       ⚠️ This package is custom-specified by the notebook's author (not on public PyPI).")
                print("          If it's unavailable, contact the author for its current location.")
            print(f"       ├─ Author Verified Version: {ver or 'unspecified'}")
            if flags:
                print(f"       ├─ Scoped Flags: {' '.join(flags)}")
            print(f"       └─ Error: {err_snippet}\n")

    if raw_installs:
        print("\n📎 Installing non-standard sources (git/URL/local file)...")
        print("   These are installed exactly as specified but can't be verified against PyPI.")
        print("   You are responsible for ensuring anyone running this notebook has access to the same resource.\n")
        for raw_idx, raw_spec in enumerate(raw_installs, start=idx + 1):
            print(f"[{raw_idx}/{total_deps}] 📦 Installing (raw): {raw_spec}")
            sys.stdout.flush()
            raw_cmd = [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check", "--no-warn-script-location", raw_spec]
            raw_returncode, raw_captured = _run_pip_subprocess(raw_cmd, timeout)
            if raw_returncode == 0:
                passed_count += 1
                print(f"    ✅ {raw_spec} installed successfully")
            else:
                failed_packages.append((raw_spec, "", [], "\n".join(raw_captured)))
                print(f"    ❌ {raw_spec} failed to install (exit code {raw_returncode})")
                print("       ⚠️ This is a custom-specified source (git/URL/local file), not a standard PyPI package.")
                print("          If it's unreachable, contact the notebook's author for its current location.")

    print("\n" + "=" * 60)
    if not failed_packages:
        print(f"✅ Setup complete! All {passed_count}/{total_deps} dependencies verified.")
    else:
        print(f"⚠️ Setup completed with issues: {passed_count}/{total_deps} packages installed.")
        print("Troubleshooting Steps:")
        print("1. Internet Access: Ensure your notebook environment has active internet access.")
        print("2. Unpinned Installs: Test installing failed libraries manually: '!pip install <pkg>'")
        print(f"3. Troubleshooting Steps: For a detailed guide on resolving setup errors, see: {HELP_URL}")

    if any_install_performed:
        print("\n⚠️ Note: You may need to restart the kernel to use updated packages.")
    print("=" * 60)

    return InstallResult(total=total_deps, installed=passed_count, failed=[spec for spec, *_ in failed_packages])





