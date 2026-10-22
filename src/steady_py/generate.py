"""Generating the manifest and setup cells: the pinned manifest with its generation-time drift
validation, the managed markdown and setup cells, the universal manifest for a directory, and writing
the locked notebook or reading its manifest back."""
import ast
import copy
import importlib.metadata
import json
import logging
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, TypedDict, Union

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from steady_py import accelerator, analyze, declarations, delta, drift, reconcile, installed, models, pypi, resolution, scanning
from steady_py.constants import (
    INSTALLABLE_STATUSES, UNRECORDED_STATUSES, DependencyStatus, FetchStatus, Provenance, ReportKind, SETUP_MARKDOWN_HEADING,
    TOOL_VERSION,
)
from steady_py.models import (
    Cell, Declaration, DeclarationEvent, DependencyEntry, DiagnosticEvent, GpuInfo, InstallLine, LocalModule, ManifestError, ManifestPackage, NotebookAnalysisReport, PinnedDependency,
    RawInstall, SteadyPyManifest,
)

logger = logging.getLogger("steady_py.generate")

# Cell 2 groups these commented entries under a heading, written before the first of each (G17).
SECTION_HEADERS: Dict[str, str] = {
    DependencyStatus.AUXILIARY_TOOL: "\n# --- AUXILIARY TOOL INSTALLS (harvested from cell magics) ---",
    DependencyStatus.WRITEFILE_SCRIPT: "\n# --- WRITEFILE SCRIPT DEPENDENCIES ---",
}


class BlueprintResult(TypedDict):
    """Cell blueprint output strings for Cell 1 (Markdown) and Cell 2 (Python script)."""
    step1_markdown: str
    step2_code: str
    drift_report: "drift.DriftCheckReport"
    declaration_events: List[DeclarationEvent]    # what install and ignore declarations changed
    declaration_warnings: List[DiagnosticEvent]
    line_verdicts: List["reconcile.LineVerdict"]  # install lines naming a package Cell 2 installs
    line_proposals: List[DiagnosticEvent]         # declarations proposed for lines Cell 2 doesn't reproduce


# --- Manifest extraction ----------------------------------------------------
# Parses a previously-generated STEADY_PY_MANIFEST back out of a .ipynb or .py
# file. No execution: ast.parse + ast.literal_eval only. "No manifest present"
# is not an error -- it's the expected state for a pre-feature notebook.

def _manifest_assignment(tree: ast.AST) -> Optional[ast.Assign]:
    """The `STEADY_PY_MANIFEST = ...` assignment that defines a manifest, if `tree` has one."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "STEADY_PY_MANIFEST" for t in node.targets
        ):
            return node
    return None


@dataclass
class ManifestRead:
    """What reading a file's manifest found: the manifest, or why there is none. No manifest at all
    is not an error (a notebook from before manifests is a valid state). `newer` is True when a newer
    steady-py wrote it, so this one must not replace it."""
    manifest: Optional[SteadyPyManifest] = None
    error: Optional[str] = None
    newer: bool = False


def extract_manifest_from_file(path: str) -> Tuple[Optional[SteadyPyManifest], Optional[str]]:
    """(manifest, error): read_manifest_file's result as a pair."""
    read = read_manifest_file(path)
    return read.manifest, read.error


def read_manifest_file(path: str) -> ManifestRead:
    """The manifest a notebook or .py file carries. A real problem (an unreadable file, a corrupted
    embedded literal, a manifest this steady-py can't read) is an error naming the file. In a
    notebook the manifest is read from the first code cell that defines it, so IPython syntax or a
    broken cell elsewhere can't hide it (K1).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        nb_data = json.loads(text) if path.endswith(".ipynb") else None
    except (OSError, json.JSONDecodeError) as e:
        return ManifestRead(error=f"Could not read {path}: {e}")

    if nb_data is not None:
        cells = sorted(scanning.read_notebook_cells(nb_data.get("cells", [])), key=lambda c: c.position or 0)
        return read_manifest_cells(cells, path)
    try:
        assignment = _manifest_assignment(ast.parse(text))
    except SyntaxError as e:
        return ManifestRead(error=f"Could not parse {path} as Python source: {e}")
    return _manifest_read(assignment, path)


def read_manifest_cells(cells: Sequence[Cell], where: str) -> ManifestRead:
    """The manifest from the first of `cells` that defines one, in the order given: a notebook file's
    cells in notebook order, or a live session's newest first, so the last executed Cell 2 counts.
    `where` names the source in errors."""
    unparsed: Optional[str] = None
    for cell in cells:
        parsed = scanning.parse_cell(cell)
        if parsed.kind != "PYTHON":
            continue
        if parsed.tree is None:
            if unparsed is None and "STEADY_PY_MANIFEST" in cell.source:
                unparsed = cell.label
            continue
        assignment = _manifest_assignment(parsed.tree)
        if assignment is not None:
            return _manifest_read(assignment, where)
    if unparsed is not None:
        return ManifestRead(error=f"Could not parse {unparsed} of {where}, which mentions STEADY_PY_MANIFEST, as Python source.")
    return ManifestRead()


def _manifest_read(assignment: Optional[ast.Assign], path: str) -> ManifestRead:
    manifest_dict = None
    if assignment is not None:
        try:
            manifest_dict = ast.literal_eval(assignment.value)
        except (ValueError, SyntaxError) as e:
            return ManifestRead(error=f"STEADY_PY_MANIFEST found in {path} but is not a valid literal: {e}")

    if manifest_dict is None:
        return ManifestRead()  # no manifest present -- not an error

    try:
        return ManifestRead(manifest=SteadyPyManifest.from_literal(manifest_dict))
    except ManifestError as e:
        return ManifestRead(error=f"The manifest in {path} can't be read: {e}.", newer=e.newer)


def _package(entry: DependencyEntry) -> Optional[ManifestPackage]:
    """The manifest record of one dependency entry, or None for an entry the manifest doesn't record
    as a package (UNRECORDED_STATUSES)."""
    if entry.status in UNRECORDED_STATUSES or not entry.name:
        return None
    base, extras = installed.split_pin_extras(entry.name)
    status = entry.status
    if status == DependencyStatus.PINNED and entry.is_comment:
        status = DependencyStatus.NOT_INSTALLED
    provenance: Dict[str, str] = {}
    version = entry.version or None
    url = entry.direct_url or None if status == DependencyStatus.DIRECT_REFERENCE else None
    if version:
        provenance["version"] = entry.version_source
    if url:
        provenance["url"] = Provenance.ENVIRONMENT
    options = models.option_pairs(entry.flags)
    if options:
        provenance["options"] = Provenance.NOTEBOOK
    return ManifestPackage(name=canonicalize_name(base), display_name=base, status=status, version=version,
                           extras=tuple(sorted(extras)), url=url, options=options, provenance=provenance)


def _add(packages: Dict[Tuple[str, Tuple[str, ...]], ManifestPackage], package: ManifestPackage) -> None:
    """Adds `package`, keeping the first entry for a key unless a later one is one Cell 2 installs and
    the first isn't (a module imported by the notebook and by a %%writefile script, say)."""
    existing = packages.get(package.key)
    if existing is None or (package.status in INSTALLABLE_STATUSES and existing.status not in INSTALLABLE_STATUSES):
        packages[package.key] = package


def _direct_source(raw: RawInstall, packages: Dict[Tuple[str, Tuple[str, ...]], ManifestPackage]) -> None:
    """Records an install line's direct source (a URL, `name @ url` or local path) as written (CH3).
    It replaces a package the environment records as installed from the same source, so the line's
    own text and options are what Cell 2 installs."""
    try:
        req: Optional[Requirement] = Requirement(raw.spec)
    except InvalidRequirement:
        req = None
    name = canonicalize_name(req.name) if req is not None and req.url else ""
    provenance = {"url": Provenance.NOTEBOOK}
    options = models.option_pairs(raw.flags)
    if options:
        provenance["options"] = Provenance.NOTEBOOK
    for key, existing in list(packages.items()):
        if existing.status == DependencyStatus.DIRECT_REFERENCE and existing.url and (
                installed.same_direct_source(existing.url, raw.spec) or (name and existing.name == name)):
            packages[key] = ManifestPackage(
                name=existing.name, display_name=existing.display_name, status=DependencyStatus.DIRECT_REFERENCE,
                version=existing.version, extras=existing.extras, url=raw.spec, options=options,
                provenance={**existing.provenance, **provenance})
            return
    _add(packages, ManifestPackage(name=name, display_name=req.name if name and req is not None else "",
                                   status=DependencyStatus.DIRECT_REFERENCE, url=raw.spec, options=options,
                                   provenance=provenance))


def _is_custom_sourced(package: ManifestPackage) -> bool:
    """True for an index pin that isn't on public PyPI: a local version identifier, or not found
    there. pypi.fetch_pypi_package_metadata is memoized, so the later pin checks reuse the lookup."""
    if package.status != DependencyStatus.PINNED or not package.version:
        return False
    return (installed.has_local_version_identifier(package.version)
            or pypi.fetch_pypi_package_metadata(package.display_name or package.name).status != FetchStatus.FOUND)


def _declared_version(name: str) -> Optional[str]:
    """The version an install declaration without one pins: the installed version, else PyPI's latest."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        pass
    found = pypi.fetch_pypi_package_metadata(name)
    return found.latest_version if found.status == FetchStatus.FOUND else None


def generate_production_blueprint(
    manifest_items: Sequence[Union[DependencyEntry, PinnedDependency, str]],
    full_freeze_lines: Optional[List[str]] = None,
    local_tagged_info: Optional[List[Tuple[str, List[str]]]] = None,
    gpu_info: Optional[GpuInfo] = None,
    install_timeout: int = 120,
    raw_installs: Optional[List[RawInstall]] = None,
    previous: Optional[SteadyPyManifest] = None,
    declared: Sequence[Declaration] = (),
    install_lines: Sequence[InstallLine] = (),
) -> BlueprintResult:
    """Assembles Cell 1 Markdown and Cell 2 Python code using structured DependencyEntry objects.

    `previous` is the manifest the notebook already carries: entries and the manifest keep their
    change dates where nothing changed (delta.settle).
    """
    py_major, py_minor = sys.version_info.major, sys.version_info.minor
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    packages: Dict[Tuple[str, Tuple[str, ...]], ManifestPackage] = {}
    comment_lines: List[str] = []
    local_modules: List[LocalModule] = []

    headed: Set[str] = set()
    for item in manifest_items:
        if isinstance(item, DependencyEntry):
            if item.is_comment:
                if item.status in SECTION_HEADERS and item.status not in headed:
                    headed.add(item.status)
                    comment_lines.append(SECTION_HEADERS[item.status])
                comment_lines.append(item.comment_text)
                if item.status == DependencyStatus.LOCAL_MODULE and item.anchor:
                    local_modules.append(LocalModule(item.name, item.anchor, provenance={"anchor": Provenance.ENVIRONMENT}))
            package = _package(item)
            if package is not None:
                _add(packages, package)
        elif isinstance(item, PinnedDependency):
            _add(packages, _package(DependencyEntry(name=item.name, version=item.version, flags=list(item.flags))) or
                 ManifestPackage(item.name, DependencyStatus.PINNED))
        elif isinstance(item, str):
            clean_item = item.strip()
            if clean_item.startswith("#") or clean_item.startswith("--"):
                comment_lines.append(clean_item)
                continue
            name, _, ver = clean_item.partition("==")
            package = _package(DependencyEntry(name=name, version=ver))
            if package is not None:
                _add(packages, package)
    for raw in raw_installs or []:
        _direct_source(raw, packages)
    declared_packages, declaration_events, declaration_warnings = declarations.apply_to_packages(
        list(packages.values()), declared, _declared_version)
    declaration_warnings += declarations.reachable_ignores(declared_packages, declared)
    line_verdicts = reconcile.classify(install_lines, declared_packages)
    conflicted = {v.package.name for v in line_verdicts if v.conflicting}
    guarded = reconcile.guards(install_lines)
    declared_packages = [replace(p, conflicting_line=p.name in conflicted, guard=guarded.get(p.name)) for p in declared_packages]
    line_proposals = reconcile.proposals(install_lines, declared_packages, declared)
    package_list = [replace(p, custom_sourced=_is_custom_sourced(p)) for p in declared_packages]

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

    # Check pins against live PyPI at generation time, not only via a later,
    # separate --check-drift run -- catching a bad pin now is strictly better
    # than freezing it into a "reproducible" cell that never worked. The
    # manifest is still produced either way (this tool never withholds
    # output); findings are surfaced to the caller for a loud warning. Computed
    # before the manifest is final so they can be recorded inside it.
    fresh = SteadyPyManifest(
        python_version={"major": py_major, "minor": py_minor},
        packages=package_list,
        gpu=gpu_info.to_dict() if gpu_info else None,
        local_modules=local_modules,
        declarations=list(declared),
    )
    generation_findings = drift.run_pin_checks(fresh.pins(), fresh.python_version)
    fresh.baseline = drift.build_baseline(generation_findings)
    manifest = delta.settle(previous, fresh, now)
    manifest.compute_and_set_hash()
    timestamp = manifest.generated_at

    drift_report = drift.build_drift_check_report("", manifest, generation_findings, kind=ReportKind.VALIDATION)

    freeze_block_code = ""
    if full_freeze_lines:
        freeze_lines_repr = repr(full_freeze_lines)
        freeze_block_code = f"\n# --- FULL FREEZE FALLBACK BLOCK ---\nFULL_FREEZE_FALLBACK = {freeze_lines_repr}\n"

    pin = installed.self_pin(TOOL_VERSION)
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
    print(f"⚠️ Could not install the pinned {pin} helper (it may not be released yet, or its source may be unreachable).")
    print((_result.stdout + _result.stderr)[-2000:])
    print("Proceeding with whatever steady_py is already available, if any...")

import steady_py
steady_py.install(STEADY_PY_MANIFEST, timeout={install_timeout})"""

    return {
        "step1_markdown": step1_markdown,
        "step2_code": step2_code,
        "drift_report": drift_report,
        "declaration_events": declaration_events,
        "declaration_warnings": declaration_warnings,
        "line_verdicts": line_verdicts,
        "line_proposals": line_proposals,
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
        tree = scanning.parse_cell(Cell(source=source)).tree
        return tree is not None and _manifest_assignment(tree) is not None
    return False


def setup_cell_position(raw_cells: Sequence[Dict[str, Any]]) -> Optional[int]:
    """The notebook position of the prior Cell 2 (the first setup code cell), or None without one."""
    return next((i for i, c in enumerate(raw_cells) if c.get("cell_type") == "code" and is_prior_setup_cell(c)), None)


def _managed_role(cell: Dict[str, Any]) -> Optional[str]:
    """The role a managed cell's metadata tags it with, if any."""
    meta = cell.get("metadata")
    tool_meta = meta.get("steady_py") if isinstance(meta, dict) else None
    role = tool_meta.get("role") if isinstance(tool_meta, dict) and tool_meta.get("managed") is True else None
    return role if isinstance(role, str) else None


def create_managed_cells(blueprint: BlueprintResult, ids: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """Creates cell dicts stamped with steady_py managed metadata and RFC-compliant IDs. `ids` maps a
    role to the ID its cell already has, which is kept, so a rewrite that changes nothing changes no
    ID (P5)."""
    ids = ids or {}
    cell1 = {
        "cell_type": "markdown",
        "id": ids.get("setup_markdown") or f"spy-{uuid.uuid4().hex[:6]}",
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
        "id": ids.get("setup_code") or f"spy-{uuid.uuid4().hex[:6]}",
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
    editables: Optional[Mapping[str, str]] = None,
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
        ctx = analyze.join_context(res.path, frozen_env, pkg_dist_map, editables, repo_map.target_dir)
        entries, _ = resolution.build_manifest_entries(res.imports, res.submodules, ctx, guarded_imports=res.guarded_imports)
        pinned_entries_set.update(entries)

        aux_entries = resolution.build_auxiliary_tool_entries(res.harvested_pkgs, res.imports, frozen_env)
        for aux in aux_entries:
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
    previous: Optional[SteadyPyManifest] = None,
    declarations: Sequence[Declaration] = (),
) -> BlueprintResult:
    """The two setup cells and the generation-time validation for an analyzed notebook.

    `hardware` is the probed accelerator; it is narrowed to the frameworks this notebook imports.
    `previous` is the manifest the notebook already carries, if any.
    """
    return generate_production_blueprint(
        report.dependencies,
        full_freeze_lines=full_freeze_lines,
        local_tagged_info=report.local_tagged,
        gpu_info=accelerator.resolve_notebook_gpu_info(scan_res.imports, hardware),
        install_timeout=install_timeout,
        raw_installs=scan_res.raw_installs,
        previous=previous,
        declared=declarations,
        install_lines=report.install_lines,
    )


def output_path(source: Path, suffix: Optional[str] = None, in_place: bool = False, root_dir: Optional[str] = None,
                output_dir: Optional[str] = None) -> Path:
    """The file write_locked_notebook writes for `source`: the source itself (in_place), its place
    under output_dir (mirroring its folder below root_dir), or beside it with a suffix."""
    if in_place:
        return source
    if output_dir:
        dest_dir = Path(output_dir)
        if root_dir and Path(root_dir).exists():
            try:
                dest_dir = dest_dir / source.parent.relative_to(Path(root_dir))
            except ValueError:
                pass
        return dest_dir / f"{source.stem}{suffix if suffix is not None else ''}.ipynb"
    return source.parent / f"{source.stem}{suffix if suffix is not None else '_merged'}.ipynb"


def write_locked_notebook(
    scan_res: analyze.NotebookScanResult,
    blueprint: BlueprintResult,
    suffix: Optional[str] = None,
    in_place: bool = False,
    root_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> Optional[Path]:
    """Writes a notebook with the blueprint's setup cells in place of any prior ones (idempotent),
    either over the source (in_place), into output_dir, or beside the source with a suffix.
    Returns the path written, or None when an in-place write would leave the notebook as it is, in
    which case nothing is written (P5)."""
    with open(scan_res.path, 'r', encoding='utf-8') as f:
        nb_data = json.load(f)
    original = copy.deepcopy(nb_data)

    cells = nb_data.get("cells", [])
    prior_ids = {role: c["id"] for c in cells if (role := _managed_role(c)) and isinstance(c.get("id"), str)}
    managed_cells = create_managed_cells(blueprint, prior_ids)

    # Idempotent filter: strip prior setup blocks, tagged or not (see is_prior_setup_cell)
    non_managed_cells = [c for c in cells if not is_prior_setup_cell(c)]
    nb_data["cells"] = managed_cells + non_managed_cells
    if in_place and nb_data == original:
        return None

    target_path = output_path(scan_res.path, suffix, in_place, root_dir, output_dir)
    target_path.parent.mkdir(parents=True, exist_ok=True)
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
) -> Tuple[Optional[Path], "drift.DriftCheckReport"]:
    """Writes per-notebook locked file or replaces setup cells in-place idempotently.
    Returns the written path (None when an in-place write changed nothing) and the
    generation-time drift-check report."""
    report = analyze.build_single_notebook_report(scan_res, frozen_env, pkg_dist_map, batch_hw_cache, root_dir=root_dir)
    previous = read_manifest_file(str(scan_res.path)).manifest
    blueprint = build_blueprint_for_notebook(scan_res, report, batch_hw_cache, install_timeout=install_timeout,
                                             previous=previous)
    target_path = write_locked_notebook(
        scan_res, blueprint, suffix=suffix, in_place=in_place, root_dir=root_dir, output_dir=output_dir
    )
    return target_path, blueprint["drift_report"]
