"""Generating the manifest and setup cells: the pinned manifest with its generation-time drift
validation, the managed markdown and setup cells, the universal manifest for a directory, and writing
the locked notebook or reading its manifest back."""
import ast
import json
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, TypedDict, Union

from steady_py import accelerator, analyze, drift, installed, localmodules, pypi, resolution, scanning
from steady_py.constants import DependencyStatus, FetchStatus, ReportKind, SETUP_MARKDOWN_HEADING, TOOL_VERSION
from steady_py.models import DependencyEntry, GpuInfo, NotebookAnalysisReport, PinnedDependency, SteadyPyManifest

logger = logging.getLogger("steady_py.generate")


class BlueprintResult(TypedDict):
    """Cell blueprint output strings for Cell 1 (Markdown) and Cell 2 (Python script)."""
    step1_markdown: str
    step2_code: str
    drift_report: "drift.DriftCheckReport"


# --- Manifest extraction ----------------------------------------------------
# Parses a previously-generated STEADY_PY_MANIFEST back out of a .ipynb or .py
# file. No execution: ast.parse + ast.literal_eval only. "No manifest present"
# is not an error -- it's the expected state for a pre-feature notebook.

def extract_manifest_from_file(path: str) -> Tuple[Optional[SteadyPyManifest], Optional[str]]:
    """Returns (manifest, error). No manifest found -> (None, None), not an error.
    A real problem (unreadable file, corrupted embedded literal) -> (None, "message").
    """
    try:
        if path.endswith(".ipynb"):
            with open(path, "r", encoding="utf-8") as f:
                nb_data = json.load(f)
            cell_sources = [
                "".join(cell.get("source", []))
                for cell in nb_data.get("cells", [])
                if cell.get("cell_type") == "code"
            ]
            cleaned_cells = []
            for cell_source in cell_sources:
                cell_type, clean_body = scanning.classify_cell_source(cell_source)
                if cell_type in {"SHELL_SCRIPT", "WRITEFILE"}:
                    continue
                cleaned_cells.append("\n".join(
                    "" if (line.strip().startswith('%') or line.strip().startswith('!')) else line
                    for line in clean_body.splitlines()
                ))
            source = "\n".join(cleaned_cells)
        else:
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
    except (OSError, json.JSONDecodeError) as e:
        return None, f"Could not read {path}: {e}"

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return None, f"Could not parse {path} as Python source: {e}"

    manifest_dict = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "STEADY_PY_MANIFEST" for t in node.targets
        ):
            try:
                manifest_dict = ast.literal_eval(node.value)
            except (ValueError, SyntaxError) as e:
                return None, f"STEADY_PY_MANIFEST found in {path} but is not a valid literal: {e}"
            break

    if manifest_dict is None:
        return None, None  # no manifest present -- not an error

    try:
        return SteadyPyManifest.from_literal(manifest_dict), None
    except TypeError as e:
        return None, f"STEADY_PY_MANIFEST found in {path} but has an unexpected shape: {e}"


def generate_production_blueprint(
    manifest_items: Sequence[Union[DependencyEntry, PinnedDependency, str]], 
    full_freeze_lines: Optional[List[str]] = None, 
    local_tagged_info: Optional[List[Tuple[str, List[str]]]] = None, 
    gpu_info: Optional[GpuInfo] = None,
    install_timeout: int = 120,
    raw_installs: Optional[List[str]] = None
) -> BlueprintResult:
    """Assembles Cell 1 Markdown and Cell 2 Python code using structured DependencyEntry objects."""
    py_major, py_minor = sys.version_info.major, sys.version_info.minor
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    normalized_items: List[PinnedDependency] = []
    comment_lines: List[str] = []
    local_modules_captured: List[Dict[str, str]] = []
    direct_reference_specs: List[str] = []

    for item in manifest_items:
        if isinstance(item, DependencyEntry):
            if item.is_comment:
                comment_lines.append(item.comment_text)
                if item.status == DependencyStatus.DIRECT_REFERENCE and item.direct_url:
                    direct_reference_specs.append(item.direct_url)
                if item.status == DependencyStatus.LOCAL_MODULE and item.anchor:
                    local_modules_captured.append({"name": item.name, "anchor": item.anchor})
            else:
                normalized_items.append(item.to_pin())
        elif isinstance(item, PinnedDependency):
            normalized_items.append(item)
        elif isinstance(item, str):
            clean_item = item.strip()
            if clean_item.startswith("#") or clean_item.startswith("--"):
                comment_lines.append(clean_item)
                continue
            parts = clean_item.split("==")
            name = parts[0]
            ver = parts[1] if len(parts) > 1 else ""
            normalized_items.append(PinnedDependency(name=name, version=ver))

    gpu_markdown_section = ""
    if gpu_info and gpu_info.has_gpu:
        dev_name = gpu_info.device_name
        active_fw = gpu_info.active_framework or "Framework"
        gpu_markdown_section = (
            f"- **Hardware Acceleration:** This notebook was created using a GPU accelerator (`{dev_name}`, verified via {active_fw}).\n"
            f"  If execution feels slow, ensure your runtime has a GPU accelerator enabled in environment settings."
        )

    local_builds_section = ""
    if local_tagged_info:
        bullet_lines = []
        for pkg, urls in local_tagged_info:
            bullet_lines.append(f"  • `{pkg}`")
            if urls:
                for u in urls:
                    bullet_lines.append(f"    Download index: `{u}`")
            else:
                bullet_lines.append("    ⚠️ Specific hardware build tag detected. If installation fails, ensure your runtime matches this build.")
        local_builds_section = f"- **Specific Package Builds Detected:** The following package(s) use custom or hardware-specific builds:\n" + "\n".join(bullet_lines)

    markdown_lines = [
        SETUP_MARKDOWN_HEADING,
        f"This notebook includes verified dependencies to ensure reproducible execution.\n",
        "- **Automatic Setup:** Cell 2 verifies Python version compatibility and installs verified package versions sequentially."
    ]
    
    if gpu_markdown_section:
        markdown_lines.append(gpu_markdown_section)
    if local_builds_section:
        markdown_lines.append(local_builds_section)
        
    markdown_lines.append("- **Network Notice:** Active internet access is required to download uncached packages.")

    step1_markdown = "\n".join(markdown_lines)
    
    comments_block = ""
    if comment_lines:
        comments_block = "\n# Informational notes & uninstalled fallbacks:\n" + "\n".join(comment_lines) + "\n"

    # Classify custom-sourced pins (local-version-identifier or not found on PyPI)
    # up front so the runtime failure path can point to the right guidance if
    # install ever fails. pypi.fetch_pypi_package_metadata is memoized, so this costs
    # nothing extra -- drift.run_pin_checks below reaches the same pins.
    custom_sourced_names: List[str] = []
    for dep in normalized_items:
        name, version = dep.name, dep.version
        if not name or not version:
            continue
        bare_name, _extra = installed.split_pin_name(name)
        if installed.has_local_version_identifier(version) or pypi.fetch_pypi_package_metadata(bare_name).status != FetchStatus.FOUND:
            custom_sourced_names.append(name)

    # Installed from a remote direct reference with no matching install line in the
    # notebook itself: carry the recorded source, unless the notebook already names it.
    merged_raw_installs: List[str] = list(raw_installs) if raw_installs else []
    for spec in direct_reference_specs:
        if not any(installed.same_direct_source(spec, existing) for existing in merged_raw_installs):
            merged_raw_installs.append(spec)

    # Check pins against live PyPI at generation time, not only via a later,
    # separate --check-drift run -- catching a bad pin now is strictly better
    # than freezing it into a "reproducible" cell that never worked. The
    # manifest is still produced either way (this tool never withholds
    # output); findings are surfaced to the caller for a loud warning. Computed
    # before the manifest exists so they can be recorded inside it.
    python_version = {"major": py_major, "minor": py_minor}
    generation_findings = drift.run_pin_checks(normalized_items, python_version)

    manifest = SteadyPyManifest(
        python_version=python_version,
        dependencies=normalized_items,
        gpu=gpu_info.to_dict() if gpu_info else None,
        generated_at=timestamp,
        raw_installs=merged_raw_installs,
        custom_sourced=custom_sourced_names,
        local_modules=local_modules_captured,
        baseline=drift.build_baseline(generation_findings),
    )
    manifest.compute_and_set_hash()

    drift_report = drift.build_drift_check_report("", manifest, generation_findings, kind=ReportKind.VALIDATION)

    freeze_block_code = ""
    if full_freeze_lines:
        freeze_lines_repr = repr(full_freeze_lines)
        freeze_block_code = f"\n# --- FULL FREEZE FALLBACK BLOCK ---\nFULL_FREEZE_FALLBACK = {freeze_lines_repr}\n"

    pin = f"steady-py=={TOOL_VERSION}"
    step2_code = f"""# =====================================================================
# VERIFIED ENVIRONMENT DEPENDENCIES ({timestamp})
# =====================================================================
import subprocess
import sys

# Reproducibility manifest (dependencies, Python target, GPU context, integrity hash)
STEADY_PY_MANIFEST = {repr(manifest.to_dict())}
{comments_block}{freeze_block_code}
_result = subprocess.run(
    [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check", "{pin}"],
    capture_output=True, text=True,
)
if _result.returncode != 0:
    print(f"⚠️ Could not install the pinned {pin} helper (it may not be released yet).")
    print((_result.stdout + _result.stderr)[-2000:])
    print("Proceeding with whatever steady_py is already available, if any...")

import steady_py
steady_py.install(STEADY_PY_MANIFEST, timeout={install_timeout})"""

    return {
        "step1_markdown": step1_markdown,
        "step2_code": step2_code,
        "drift_report": drift_report,
    }


def is_prior_setup_cell(cell: Dict[str, Any]) -> bool:
    """True if `cell` is a previously generated setup cell that a fresh run must replace.

    The managed tag is the primary signal, but it is not sufficient: cells pasted
    from the live-kernel flow never carry it, and metadata can be lost on save.
    Left in place, an untagged old manifest would coexist with the new one and
    run after it. So content is checked as well: a code cell that assigns
    STEADY_PY_MANIFEST (the same definition extract_manifest_from_file uses for
    "this is the manifest"), or a markdown cell that begins with the generated
    setup heading. Cells that merely mention either are not matched.
    """
    meta = cell.get("metadata")
    tool_meta = meta.get("steady_py") if isinstance(meta, dict) else None
    if isinstance(tool_meta, dict) and tool_meta.get("managed") is True:
        return True

    source = cell.get("source", "")
    if isinstance(source, list):
        source = "".join(source)
    if not isinstance(source, str):
        return False

    cell_type = cell.get("cell_type")
    if cell_type == "markdown":
        return source.lstrip().startswith(SETUP_MARKDOWN_HEADING)
    if cell_type == "code":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return False
        return any(
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "STEADY_PY_MANIFEST" for t in node.targets)
            for node in ast.walk(tree)
        )
    return False


def create_managed_cells(blueprint: BlueprintResult) -> List[Dict[str, Any]]:
    """Creates cell dicts stamped with steady_py managed metadata and RFC-compliant IDs."""
    cell1 = {
        "cell_type": "markdown",
        "id": f"spy-{uuid.uuid4().hex[:6]}",
        "metadata": {
            "steady_py": {
                "managed": True,
                "role": "setup_markdown"
            }
        },
        "source": [line + "\n" for line in blueprint["step1_markdown"].splitlines()]
    }
    cell2 = {
        "cell_type": "code",
        "execution_count": None,
        "id": f"spy-{uuid.uuid4().hex[:6]}",
        "metadata": {
            "steady_py": {
                "managed": True,
                "role": "setup_code"
            }
        },
        "outputs": [],
        "source": [line + "\n" for line in blueprint["step2_code"].splitlines()]
    }
    return [cell1, cell2]


def generate_universal_manifest(
    repo_map: analyze.RepoEnvironmentMap,
    frozen_env: Dict[str, str],
    pkg_dist_map: Mapping[str, List[str]],
    skipped: Optional[Sequence[Tuple[str, str]]] = None,
) -> str:
    """Generates content string for universal manifest.

    `skipped` is (path, reason) for each notebook that could not be read. They are not covered by
    the file, so it opens with a comment naming them: anyone who opens it or diffs it sees the gap.
    """
    lines = []
    if skipped:
        lines.append(f"# !!! INCOMPLETE: {len(skipped)} notebook(s) could not be read and are NOT covered by this file:")
        for path, reason in skipped:
            lines.append(f"#   {path}: {' '.join(str(reason).split())}")
    lines.append("# =====================================================================")
    lines.append("# REPOSITORY UNIVERSAL DEPENDENCY MANIFEST")
    lines.append(f"# Target Directory: {repo_map.target_dir}")
    lines.append(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    primary_url, url_reason = analyze.select_primary_index_url(repo_map.url_to_notebooks)
    if primary_url:
        lines.append("#")
        lines.append(f"# Primary Download Index: {primary_url}")
        lines.append(f"# Selection Rule: {url_reason}")

    lines.append("# =====================================================================\n")

    if repo_map.url_to_notebooks:
        for url in sorted(repo_map.url_to_notebooks.keys()):
            lines.append(f"--extra-index-url {url}")

    pinned_entries_set: Set[str] = set()
    for res in repo_map.scan_results:
        nb_local_ctx = localmodules.LocalModuleContext(str(res.path.parent), repo_map.target_dir)
        entries, _ = resolution.build_manifest_entries(
            res.imports, 
            res.submodules, 
            frozen_env, 
            pkg_dist_map, 
            guarded_imports=res.guarded_imports,
            local_ctx=nb_local_ctx
        )
        pinned_entries_set.update(entries)

        aux_entries = resolution.build_auxiliary_tool_entries(res.harvested_pkgs, res.imports, frozen_env)
        for aux in aux_entries:
            if not aux.comment_text.startswith("\n# ---"):
                pinned_entries_set.add(aux.comment_text)

    for entry in sorted(pinned_entries_set):
        lines.append(entry)

    return "\n".join(lines)


def build_blueprint_for_notebook(
    scan_res: analyze.NotebookScanResult,
    report: NotebookAnalysisReport,
    hardware: Optional[GpuInfo],
    install_timeout: int = 120,
    full_freeze_lines: Optional[List[str]] = None,
) -> BlueprintResult:
    """The two setup cells and the generation-time validation for an analyzed notebook.

    `hardware` is the probed accelerator; it is narrowed to the frameworks this notebook imports.
    """
    return generate_production_blueprint(
        report.dependencies,
        full_freeze_lines=full_freeze_lines,
        local_tagged_info=report.local_tagged,
        gpu_info=accelerator.resolve_notebook_gpu_info(scan_res.imports, hardware),
        install_timeout=install_timeout,
        raw_installs=scan_res.raw_installs,
    )


def write_locked_notebook(
    scan_res: analyze.NotebookScanResult,
    blueprint: BlueprintResult,
    suffix: Optional[str] = None,
    in_place: bool = False,
    root_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> Path:
    """Writes a notebook with the blueprint's setup cells in place of any prior ones (idempotent),
    either over the source (in_place), into output_dir, or beside the source with a suffix.
    Returns the path written."""
    managed_cells = create_managed_cells(blueprint)

    with open(scan_res.path, 'r', encoding='utf-8') as f:
        nb_data = json.load(f)

    cells = nb_data.get("cells", [])

    # Idempotent filter: strip prior setup blocks, tagged or not (see is_prior_setup_cell)
    non_managed_cells = [c for c in cells if not is_prior_setup_cell(c)]
    nb_data["cells"] = managed_cells + non_managed_cells

    if in_place:
        target_path = scan_res.path
    elif output_dir:
        out_base = Path(output_dir)
        stem = scan_res.path.stem
        active_suffix = suffix if suffix is not None else ""
        file_name = f"{stem}{active_suffix}.ipynb"

        if root_dir and Path(root_dir).exists():
            try:
                rel_parent = scan_res.path.parent.relative_to(Path(root_dir))
                dest_dir = out_base / rel_parent
            except ValueError:
                dest_dir = out_base
        else:
            dest_dir = out_base

        dest_dir.mkdir(parents=True, exist_ok=True)
        target_path = dest_dir / file_name
    else:
        stem = scan_res.path.stem
        active_suffix = suffix if suffix is not None else "_merged"
        target_path = scan_res.path.parent / f"{stem}{active_suffix}.ipynb"

    with open(target_path, 'w', encoding='utf-8') as f:
        json.dump(nb_data, f, indent=1)

    return target_path


def apply_output_to_notebook(
    scan_res: analyze.NotebookScanResult, 
    frozen_env: Dict[str, str], 
    pkg_dist_map: Mapping[str, List[str]], 
    batch_hw_cache: Optional[GpuInfo], 
    suffix: Optional[str] = None, 
    in_place: bool = False,
    root_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    install_timeout: int = 120
) -> Tuple[Path, "drift.DriftCheckReport"]:
    """Writes per-notebook locked file or replaces setup cells in-place idempotently.
    Returns the written path and the generation-time drift-check report."""
    report = analyze.build_single_notebook_report(scan_res, frozen_env, pkg_dist_map, batch_hw_cache, root_dir=root_dir)
    blueprint = build_blueprint_for_notebook(scan_res, report, batch_hw_cache, install_timeout=install_timeout)
    target_path = write_locked_notebook(
        scan_res, blueprint, suffix=suffix, in_place=in_place, root_dir=root_dir, output_dir=output_dir
    )
    return target_path, blueprint["drift_report"]
