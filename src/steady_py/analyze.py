"""Scanning notebooks into analysis reports: one notebook or a whole directory tree, including the
repository-wide environment map batch runs share (global imports, index URLs, local builds)."""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Set, Tuple

from steady_py import accelerator, localmodules, magics, resolution, scanning, util
from steady_py.constants import BUILD_AND_PACKAGING_TOOLS, DEFAULT_IGNORED_DIRS, DependencyStatus, PLATFORM_PSEUDO_MODULES, StatusLabel, STD_LIB
from steady_py.models import BatchAnalysisSummary, DiagnosticEvent, ExtractionResult, GpuInfo, NotebookAnalysisReport

logger = logging.getLogger("steady_py.analyze")


@dataclass
class NotebookScanResult:
    """Complete AST and metadata analysis payload for an individual notebook file."""
    path: Path
    is_python: bool
    lang_label: str
    parse_error: Optional[str] = None
    imports: List[str] = field(default_factory=list)
    submodules: Dict[str, Set[str]] = field(default_factory=dict)
    guarded_imports: Set[str] = field(default_factory=set)
    dynamic_warnings: List[DiagnosticEvent] = field(default_factory=list)
    code_sources: List[str] = field(default_factory=list)
    harvested_urls: Optional[Set[str]] = None
    writefile_imports: List[str] = field(default_factory=list)
    harvested_pkgs: Set[str] = field(default_factory=set)
    base_index_urls: Set[str] = field(default_factory=set)
    extra_index_urls: Set[str] = field(default_factory=set)
    scoped_flags: Dict[str, List[str]] = field(default_factory=dict)
    magic_warnings: List[DiagnosticEvent] = field(default_factory=list)
    magic_notices: List[DiagnosticEvent] = field(default_factory=list)
    raw_installs: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.harvested_urls is None:
            if self.code_sources:
                self.harvested_urls = magics.harvest_index_urls_from_sources(self.code_sources)
            else:
                self.harvested_urls = set()


class RepoEnvironmentMap:
    """Aggregates notebook scan results across a repository directory."""
    def __init__(self, target_dir: str) -> None:
        self.target_dir = target_dir
        self.scan_results: List[NotebookScanResult] = []
        self.non_python_files: List[NotebookScanResult] = []
        self.parse_errors: List[NotebookScanResult] = []
        self.companion_files_skipped: List[Path] = []
        self.global_imports: List[str] = []
        self.package_to_notebooks: Dict[str, List[Path]] = {}
        self.harvested_packages_to_notebooks: Dict[str, List[Path]] = {}
        self.url_to_notebooks: Dict[str, List[Path]] = {}

    def add_result(self, result: NotebookScanResult) -> None:
        if result.parse_error:
            self.parse_errors.append(result)
            return
        if not result.is_python:
            self.non_python_files.append(result)
            return

        self.scan_results.append(result)
        for imp in result.imports:
            if imp not in STD_LIB:
                if imp not in self.global_imports:
                    self.global_imports.append(imp)
                self.package_to_notebooks.setdefault(imp, []).append(result.path)

        for pkg in result.harvested_pkgs:
            if pkg not in STD_LIB:
                if pkg not in self.global_imports:
                    self.global_imports.append(pkg)
                self.harvested_packages_to_notebooks.setdefault(pkg, []).append(result.path)

        for url in result.harvested_urls or ():
            self.url_to_notebooks.setdefault(url, []).append(result.path)


def select_primary_index_url(url_to_notebooks: Dict[str, List[Path]]) -> Tuple[Optional[str], Optional[str]]:
    """Deterministically selects primary index URL based on repository frequency."""
    if not url_to_notebooks:
        return None, None

    sorted_urls = sorted(url_to_notebooks.keys())
    
    def sorting_key(url: str) -> Tuple[int, str, str]:
        notebooks = sorted([str(p) for p in url_to_notebooks[url]])
        count = len(notebooks)
        first_nb = notebooks[0] if notebooks else ""
        return (-count, first_nb, url)

    best_url = sorted(sorted_urls, key=sorting_key)[0]
    count = len(url_to_notebooks[best_url])
    total_urls = len(url_to_notebooks)
    
    if total_urls > 1:
        reason = f"Majority rule (used in {count} notebook(s); selected over {total_urls - 1} runner-up URL(s))"
    else:
        reason = f"Sole index URL harvested across batch ({count} notebook(s))"

    return best_url, reason


def build_scan_result(
    path: Path,
    ext_res: ExtractionResult,
    *,
    is_python: bool = True,
    lang_label: str = StatusLabel.PYTHON,
    parse_error: Optional[str] = None,
) -> NotebookScanResult:
    """Harvests a notebook's cell magics and commands and assembles its scan result from an extraction."""
    h_res = magics.harvest_cell_magics_and_commands(ext_res.code_sources)
    return NotebookScanResult(
        path=path,
        is_python=is_python,
        lang_label=lang_label,
        parse_error=parse_error,
        imports=ext_res.imports,
        submodules=ext_res.submodules,
        guarded_imports=ext_res.guarded_imports,
        dynamic_warnings=ext_res.dynamic_warnings,
        code_sources=ext_res.code_sources,
        harvested_urls=h_res.base_index_urls.union(h_res.extra_index_urls),
        writefile_imports=ext_res.writefile_imports,
        harvested_pkgs=h_res.harvested_packages,
        base_index_urls=h_res.base_index_urls,
        extra_index_urls=h_res.extra_index_urls,
        scoped_flags=h_res.scoped_flags,
        magic_warnings=h_res.magic_warnings,
        magic_notices=h_res.magic_notices,
        raw_installs=h_res.raw_installs,
    )


def iter_notebook_paths(target_dir: str) -> Iterator[Path]:
    """Every .ipynb under a directory, skipping hidden and ignored directories (the same rule for
    scan, snapshot and check)."""
    for root, dirs, files in os.walk(Path(target_dir)):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in DEFAULT_IGNORED_DIRS]
        for file in sorted(files):
            if file.endswith('.ipynb'):
                yield Path(root) / file


def walk_and_scan_directory(target_dir: str, skip_suffix: Optional[str] = None) -> RepoEnvironmentMap:
    """Recursively scans directory for .ipynb files in batch mode."""
    repo_map = RepoEnvironmentMap(target_dir)

    for full_path in iter_notebook_paths(target_dir):
        if skip_suffix and full_path.stem.endswith(skip_suffix):
            repo_map.companion_files_skipped.append(full_path)
            continue

        ext_res = scanning.extract_from_file(str(full_path), strict=True)

        parse_err = ext_res.error_msg if (not ext_res.success and "Skipped non-Python notebook" not in (ext_res.error_msg or "")) else None
        res = build_scan_result(
            full_path, ext_res,
            is_python=ext_res.success, lang_label=ext_res.lang_label, parse_error=parse_err,
        )
        repo_map.add_result(res)

    return repo_map


def build_single_notebook_report(
    scan_res: NotebookScanResult,
    frozen_env: Dict[str, str],
    pkg_dist_map: Mapping[str, List[str]],
    gpu_info: Optional[GpuInfo],
    root_dir: Optional[str] = None
) -> NotebookAnalysisReport:
    """Builds a complete NotebookAnalysisReport object for a single notebook."""
    local_ctx = localmodules.LocalModuleContext(str(scan_res.path.parent), root_dir)

    timeline_res = resolution.build_unified_timeline(
        scan_res.code_sources,
        frozen_env=frozen_env,
        pkg_dist_map=pkg_dist_map,
        local_ctx=local_ctx
    )

    timeline_pkgs = {util.canonicalize_pkg_name(d.name) for d in timeline_res.dependencies if d.name}
    aux_entries = resolution.build_auxiliary_tool_entries(scan_res.harvested_pkgs - timeline_pkgs, scan_res.imports, frozen_env)    
    writefile_entries = resolution.build_writefile_tool_entries(scan_res.writefile_imports, scan_res.imports, frozen_env)

    all_dep_entries, local_tagged, hw_warnings = resolution.build_dependency_entries(
        timeline_res.dependencies,
        scoped_flags=scan_res.scoped_flags,
        auxiliary_entries=aux_entries,
        writefile_entries=writefile_entries
    )

    all_warnings: List[DiagnosticEvent] = []
    all_warnings.extend(scan_res.dynamic_warnings)
    all_warnings.extend(scan_res.magic_warnings)
    all_warnings.extend(timeline_res.conflict_warnings)
    all_warnings.extend(hw_warnings)

    local_mods_detected = sorted([
        imp for imp in set(scan_res.imports)
        if localmodules.resolve_local_module(imp, local_ctx.notebook_dir, local_ctx.root_dir)
    ])
    pseudo_mods_detected = sorted(list(PLATFORM_PSEUDO_MODULES.intersection(set(scan_res.imports))))
    build_tools_detected = sorted(list(BUILD_AND_PACKAGING_TOOLS.intersection(set(scan_res.imports))))

    return NotebookAnalysisReport(
        notebook_path=str(scan_res.path),
        is_python=scan_res.is_python,
        lang_label=scan_res.lang_label,
        parse_error=scan_res.parse_error,
        dependencies=all_dep_entries,
        local_modules=local_mods_detected,
        platform_pseudo_modules=pseudo_mods_detected,
        build_and_packaging_tools=build_tools_detected,
        gpu=gpu_info,
        warnings=all_warnings,
        notices=scan_res.magic_notices,
        promotions=timeline_res.promotion_notices,
        local_tagged=local_tagged,
    )


def analyze_batch_repository(
    repo_map: RepoEnvironmentMap, 
    frozen_env: Dict[str, str], 
    pkg_dist_map: Mapping[str, List[str]], 
    batch_hw_cache: Optional[GpuInfo]
) -> BatchAnalysisSummary:
    """Aggregates dependency metrics, warnings, and index settings across repository notebooks."""
    parse_errors_list = [
        {"path": str(err_res.path), "cause": err_res.parse_error or "Unknown parse error"}
        for err_res in repo_map.parse_errors
    ]

    summary = BatchAnalysisSummary(
        target_dir=repo_map.target_dir,
        total_python_notebooks=len(repo_map.scan_results),
        non_python_count=len(repo_map.non_python_files),
        companion_skipped_count=len(repo_map.companion_files_skipped),
        parse_errors=parse_errors_list,
        batch_hw_cache=batch_hw_cache
    )

    for item in repo_map.non_python_files:
        summary.non_python_languages[item.lang_label] = (
            summary.non_python_languages.get(item.lang_label, 0) + 1
        )

    canonical_to_display: Dict[str, str] = {}
    canonical_missing_map: Dict[str, List[str]] = {}
    canonical_guarded_map: Dict[str, List[str]] = {}

    for res in repo_map.scan_results:
        nb_gpu_info = accelerator.resolve_notebook_gpu_info(res.imports, batch_hw_cache)

        nb_report = build_single_notebook_report(
            res, frozen_env, pkg_dist_map, nb_gpu_info, root_dir=repo_map.target_dir
        )
        summary.notebooks.append(nb_report)

        for dep in nb_report.dependencies:
            if not dep.is_comment and dep.version and "+" in dep.version:
                if not dep.flags:
                    summary.batch_hardware_warnings.setdefault(dep.specifier, []).append(Path(nb_report.notebook_path).name)

            if dep.is_comment:
                if dep.status in {"platform_pseudo_module", "build_tool", "local_module"}:
                    continue
                pypi_name = dep.name or (dep.comment_text.split()[1] if len(dep.comment_text.split()) > 1 else "")
                if pypi_name:
                    canon = util.canonicalize_pkg_name(pypi_name)
                    target_map = canonical_guarded_map if dep.status == DependencyStatus.GUARDED else canonical_missing_map
                    target_map.setdefault(canon, []).append(Path(nb_report.notebook_path).name)
                    display_name = pypi_name.replace("_", "-")
                    canonical_to_display.setdefault(canon, display_name)
            elif dep.name:
                summary.matched_packages.add(dep.name.split("[")[0])

        for promo in nb_report.promotions:
            if promo not in summary.promotions:
                summary.promotions.append(promo)

        for warn in res.dynamic_warnings:
            if warn not in summary.dynamic_warnings:
                summary.dynamic_warnings.append(warn)

        for warn in res.magic_warnings:
            if warn not in summary.magic_warnings:
                summary.magic_warnings.append(warn)

        for notice in res.magic_notices:
            if notice not in summary.magic_notices:
                summary.magic_notices.append(notice)

    for canon, nbs in canonical_missing_map.items():
        disp_name = canonical_to_display.get(canon, canon)
        summary.missing_packages[disp_name] = sorted(list(set(nbs)))

    for canon, nbs in canonical_guarded_map.items():
        disp_name = canonical_to_display.get(canon, canon)
        summary.guarded_packages[disp_name] = sorted(list(set(nbs)))

    primary_url, url_reason = select_primary_index_url(repo_map.url_to_notebooks)
    summary.primary_url = primary_url
    summary.primary_url_reason = url_reason

    return summary
