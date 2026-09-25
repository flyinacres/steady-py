"""Turning what a notebook imports and installs into dependency entries: the unified install/import
timeline, import-to-distribution resolution, and the pinned entries the manifest is built from."""
import importlib.metadata
import logging
import subprocess
import sys
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from steady_py import installed, localmodules, magics, scanning, util
from steady_py.constants import BUILD_AND_PACKAGING_TOOLS, DependencyStatus, IMPORT_TO_PYPI_MAP, PLATFORM_PSEUDO_MODULES, STD_LIB
from steady_py.models import DependencyEntry, DiagnosticEvent, ImportOccurrence, PromotionDetail, TimelineResult

logger = logging.getLogger("steady_py.resolution")


# =====================================================================
# UNIFIED TIMELINE ENGINE
# =====================================================================

def build_unified_timeline(
    code_sources: List[str],
    frozen_env: Dict[str, str],
    pkg_dist_map: Optional[Mapping[str, List[str]]] = None,
    is_execution_ordered: bool = True,
    local_ctx: Optional[localmodules.LocalModuleContext] = None
) -> TimelineResult:
    """
    Constructs the master sequence of DependencyEntry objects:
    - Explicit pip install occurrences anchor timeline coordinates.
    - Bare AST imports only anchor position if no explicit install was found anywhere in the notebook.
    Returns a structured TimelineResult payload.
    """
    pip_occs, _raw_installs = magics.harvest_pip_install_occurrences(code_sources)
    resolved_pips, conflict_warnings = magics.resolve_pip_occurrences(pip_occs, is_execution_ordered=is_execution_ordered)

    all_import_occs: List[ImportOccurrence] = []
    submodules_map: Dict[str, Set[str]] = {}
    guarded_set: Set[str] = set()

    for cell_idx, src in enumerate(code_sources):
        cell_imports = scanning.extract_import_occurrences_from_source(src, cell_idx=cell_idx)
        for imp in cell_imports:
            all_import_occs.append(imp)
            if imp.full_name and '.' in imp.full_name:
                submodules_map.setdefault(imp.module, set()).add(imp.full_name)
            if imp.is_guarded:
                guarded_set.add(imp.module)

    timeline_events: List[Tuple[Tuple[int, int], str, str]] = []
    seen_packages: Set[str] = set()

    # 1. Place explicit pip installs
    for norm_key, occ in resolved_pips.items():
        canon = util.canonicalize_pkg_name(occ.name)
        if norm_key == canon:
            coord = (occ.cell_idx, occ.line_idx)
            timeline_events.append((coord, "PIP", occ.name))
            seen_packages.add(canon)

    # 2. Place bare imports only if not already placed via pip install
    for imp in all_import_occs:
        norm_imp = util.canonicalize_pkg_name(imp.module)
        if imp.module.lower() in STD_LIB:
            continue
        pypi_name = IMPORT_TO_PYPI_MAP.get(imp.module, imp.module)
        canon_pypi = util.canonicalize_pkg_name(pypi_name)
        if norm_imp not in seen_packages and canon_pypi not in seen_packages:
            coord = (imp.cell_idx, imp.line_idx)
            timeline_events.append((coord, "IMPORT", imp.module))
            seen_packages.add(norm_imp)
            seen_packages.add(canon_pypi)

    timeline_events.sort(key=lambda t: t[0])

    dependencies: List[DependencyEntry] = []
    promotion_notices: List[PromotionDetail] = []

    for coord, kind, pkg_name in timeline_events:
        canon_name = util.canonicalize_pkg_name(pkg_name)
        submods = submodules_map.get(pkg_name, set())
        is_guarded = pkg_name in guarded_set

        if kind == "PIP":
            occ = resolved_pips[canon_name]
            dep_entry, promo = resolve_pypi_package_and_extras(
                occ.name, submods, frozen_env, pkg_dist_map=pkg_dist_map, is_guarded=is_guarded, local_ctx=local_ctx
            )
            dep_entry.source = "pip_command"
            if occ.version_spec and not dep_entry.is_comment:
                v_clean = occ.version_spec.lstrip("=<>!~")
                dep_entry.version = v_clean
                
                host_match = frozen_env.get(canon_name)
                if host_match and "==" in host_match:
                    host_ver = host_match.split("==", 1)[1]
                    if host_ver != v_clean:
                        logger.debug(
                            f"[Timeline] Explicit notebook pin '{pkg_name}=={v_clean}' preferred over active host version '{host_ver}'."
                        )
            dep_entry.flags = list(occ.flags)
            dependencies.append(dep_entry)
            if promo and promo not in promotion_notices:
                promotion_notices.append(promo)
        else:
            dep_entry, promo = resolve_pypi_package_and_extras(
                pkg_name, submods, frozen_env, pkg_dist_map=pkg_dist_map, is_guarded=is_guarded, local_ctx=local_ctx
            )
            dep_entry.source = "import"
            dependencies.append(dep_entry)
            if promo and promo not in promotion_notices:
                promotion_notices.append(promo)

    return TimelineResult(
        dependencies=dependencies,
        promotion_notices=promotion_notices,
        conflict_warnings=conflict_warnings
    )


# =====================================================================
# ENVIRONMENT CORRELATION & EXTRAS PROMOTION
# =====================================================================

def build_auxiliary_tool_entries(
    harvested_packages: Set[str],
    imported_packages: Any,
    frozen_env: Dict[str, str]
) -> List[DependencyEntry]:
    """Builds commented DependencyEntry instances for CLI tools installed via cell magics."""
    aux_entries: List[DependencyEntry] = []
    imported_set = {util.canonicalize_pkg_name(imp) for imp in imported_packages}
    unimported_tools = sorted([
        pkg for pkg in harvested_packages 
        if util.canonicalize_pkg_name(pkg) not in imported_set and pkg.lower() not in STD_LIB
    ])

    if not unimported_tools:
        return aux_entries

    aux_entries.append(DependencyEntry(
        is_comment=True,
        source="pip_command",
        status=DependencyStatus.AUXILIARY_TOOL,
        comment_text="\n# --- AUXILIARY TOOL INSTALLS (harvested from cell magics) ---"
    ))
    for tool in unimported_tools:
        canon_tool = util.canonicalize_pkg_name(tool)
        matched_pin = frozen_env.get(canon_tool)
        _, ver, direct_url = installed.split_frozen_pin(matched_pin) if matched_pin else ("", "", None)
        ver = ver or ""
        if direct_url:
            aux_entries.append(DependencyEntry(
                name=tool,
                source="pip_command",
                status=DependencyStatus.AUXILIARY_TOOL,
                is_comment=True,
                comment_text=f"# {tool}  (installed via cell command; {installed.direct_reference_note(direct_url)})"
            ))
        elif matched_pin:
            aux_entries.append(DependencyEntry(
                name=tool,
                version=ver,
                source="pip_command",
                status=DependencyStatus.AUXILIARY_TOOL,
                is_comment=True,
                comment_text=f"# {matched_pin}  (installed via cell command; not directly imported in Python code)"
            ))
        else:
            aux_entries.append(DependencyEntry(
                name=tool,
                version="",
                source="pip_command",
                status=DependencyStatus.AUXILIARY_TOOL,
                is_comment=True,
                comment_text=f"# {tool}  (installed via cell command; not found in active env)"
            ))

    return aux_entries


def build_writefile_tool_entries(
    writefile_imports: Any,
    primary_imports: Any,
    frozen_env: Dict[str, str]
) -> List[DependencyEntry]:
    """Builds commented DependencyEntry instances for dependencies inside %%writefile scripts."""
    entries: List[DependencyEntry] = []
    primary_set = {util.canonicalize_pkg_name(imp) for imp in primary_imports}
    script_only = sorted([
        pkg for pkg in writefile_imports 
        if util.canonicalize_pkg_name(pkg) not in primary_set and pkg.lower() not in STD_LIB
    ])

    if not script_only:
        return entries

    entries.append(DependencyEntry(
        is_comment=True,
        source="writefile_script",
        status=DependencyStatus.WRITEFILE_SCRIPT,
        comment_text="\n# --- WRITEFILE SCRIPT DEPENDENCIES ---"
    ))
    for pkg in script_only:
        pypi_name = IMPORT_TO_PYPI_MAP.get(pkg, pkg)
        canon_pypi = util.canonicalize_pkg_name(pypi_name)
        matched_pin = frozen_env.get(canon_pypi)
        _, ver, direct_url = installed.split_frozen_pin(matched_pin) if matched_pin else ("", "", None)
        ver = ver or ""
        if direct_url:
            entries.append(DependencyEntry(
                name=pypi_name,
                source="writefile_script",
                status=DependencyStatus.WRITEFILE_SCRIPT,
                is_comment=True,
                comment_text=f"# {pypi_name}  (imported inside script generated via %%writefile; {installed.direct_reference_note(direct_url)})"
            ))
        elif matched_pin:
            entries.append(DependencyEntry(
                name=pypi_name,
                version=ver,
                source="writefile_script",
                status=DependencyStatus.WRITEFILE_SCRIPT,
                is_comment=True,
                comment_text=f"# {matched_pin}  (imported inside script generated via %%writefile)"
            ))
        else:
            entries.append(DependencyEntry(
                name=pypi_name,
                version="",
                source="writefile_script",
                status=DependencyStatus.WRITEFILE_SCRIPT,
                is_comment=True,
                comment_text=f"# {pypi_name}  (imported inside script generated via %%writefile; not found in active env)"
            ))

    return entries


def resolve_pypi_package_and_extras(
    imp: str, 
    submodules_set: Set[str], 
    frozen_env: Dict[str, str], 
    pkg_dist_map: Optional[Mapping[str, List[str]]] = None,
    is_guarded: bool = False,
    local_ctx: Optional[localmodules.LocalModuleContext] = None
) -> Tuple[DependencyEntry, Optional[PromotionDetail]]:
    """Resolves top-level import to a DependencyEntry."""
    if imp in PLATFORM_PSEUDO_MODULES:
        return DependencyEntry(
            name=imp,
            status=DependencyStatus.PLATFORM_PSEUDO_MODULE,
            is_comment=True,
            comment_text=f"# {imp} (provided automatically by platform like Colab/Databricks; no install needed)"
        ), None

    if imp in BUILD_AND_PACKAGING_TOOLS:
        return DependencyEntry(
            name=imp,
            status=DependencyStatus.BUILD_TOOL,
            is_comment=True,
            comment_text=f"# {imp} (core Python build/packaging tool; excluded from requirement lockfiles)"
        ), None

    resolved_anchor = localmodules.resolve_local_module(imp, local_ctx.notebook_dir, local_ctx.root_dir) if local_ctx else None
    if resolved_anchor:
        return DependencyEntry(
            name=imp,
            status=DependencyStatus.LOCAL_MODULE,
            is_comment=True,
            comment_text=f"# {imp} (local folder/file next to notebook; ensure sibling files were shared)",
            anchor=resolved_anchor
        ), None

    pypi_name = None
    if pkg_dist_map is None and hasattr(importlib.metadata, "packages_distributions"):
        try:
            pkg_dist_map = importlib.metadata.packages_distributions()
        except Exception:
            pkg_dist_map = {}

    if pkg_dist_map and imp in pkg_dist_map:
        pypi_name = pkg_dist_map[imp][0]

    if imp == "cv2":
        pypi_name = resolve_opencv_variant(submodules_set)

    if not pypi_name:
        pypi_name = IMPORT_TO_PYPI_MAP.get(imp, imp)

    canon_pypi = util.canonicalize_pkg_name(pypi_name)
    matched_pin = frozen_env.get(canon_pypi)

    pin_version: Optional[str] = None
    direct_url: Optional[str] = None
    if matched_pin:
        _, pin_version, direct_url = installed.split_frozen_pin(matched_pin)

    if is_guarded:
        if direct_url:
            return DependencyEntry(
                name=pypi_name,
                version="",
                status=DependencyStatus.GUARDED,
                is_comment=True,
                comment_text=f"# {pypi_name} (optional or conditional dependency inside try/except block; {installed.direct_reference_note(direct_url)})"
            ), None
        if matched_pin:
            return DependencyEntry(
                name=pypi_name,
                version=pin_version or "",
                status=DependencyStatus.GUARDED,
                is_comment=True,
                comment_text=f"# {matched_pin} (optional or conditional dependency inside try/except block)"
            ), None
        return DependencyEntry(
            name=pypi_name,
            version="",
            status=DependencyStatus.GUARDED,
            is_comment=True,
            comment_text=f"# {pypi_name} (optional or conditional dependency inside try/except block)"
        ), None

    if not matched_pin:
        return DependencyEntry(
            name=pypi_name,
            version="",
            status=DependencyStatus.PINNED,
            is_comment=True,
            comment_text=f"# {pypi_name} (imported as '{imp}'; not found via pip-freeze or local file scan -- verify before assuming this is truly missing)"
        ), None

    if direct_url:
        note = installed.direct_reference_note(direct_url)
        if installed.is_local_direct_url(direct_url):
            return DependencyEntry(
                name=pypi_name,
                status=DependencyStatus.SYSTEM_PATH,
                is_comment=True,
                comment_text=f"# {pypi_name} (imported as '{imp}'; {note})"
            ), None
        return DependencyEntry(
            name=pypi_name,
            status=DependencyStatus.DIRECT_REFERENCE,
            is_comment=True,
            comment_text=f"# {pypi_name} (imported as '{imp}'; {note})",
            direct_url=direct_url
        ), None

    pkg_part, ver_part = matched_pin.split("==", 1)

    extra_tag = None
    if submodules_set:
        try:
            dist = importlib.metadata.distribution(pkg_part)
            provided_extras = dist.metadata.get_all("Provides-Extra") or []
            provided_extras_lower = {e.lower(): e for e in provided_extras}

            for sub in submodules_set:
                sub_tail = sub.split('.')[-1].lower()
                if sub_tail in provided_extras_lower:
                    extra_tag = provided_extras_lower[sub_tail]
                    break
        except importlib.metadata.PackageNotFoundError:
            pass  # not installed here, so there are no extras to match
        except Exception as e:  # extras tagging is best-effort; never let odd metadata stop the run
            logger.debug(f"Could not read Provides-Extra for '{pkg_part}': {e}", exc_info=True)

    if extra_tag:
        promoted_name = f"{pkg_part}[{extra_tag}]"
        promoted_pin = f"{promoted_name}=={ver_part}"
        notice_detail = f"💡 Extra Dependency Promotion: importing '{imp}.{extra_tag}' automatically promoted requirement to '{promoted_pin}'"
        promo = PromotionDetail(
            import_name=f"{imp}.{extra_tag}",
            promoted_name=promoted_name,
            version=ver_part,
            detail=notice_detail
        )
        return DependencyEntry(name=promoted_name, version=ver_part, status=DependencyStatus.PINNED), promo

    return DependencyEntry(name=pkg_part, version=ver_part, status=DependencyStatus.PINNED), None


@util._memoize_for_run
def build_manifest_entries(
    imports: Any, 
    submodules: Dict[str, Set[str]], 
    frozen_env: Dict[str, str], 
    pkg_dist_map: Optional[Mapping[str, List[str]]] = None,
    guarded_imports: Optional[Set[str]] = None,
    local_ctx: Optional[localmodules.LocalModuleContext] = None
) -> Tuple[List[str], List[str]]:
    """Builds string-formatted manifest lines for legacy/batch consumers while preserving order."""
    entries, promotions = build_dependency_objects(
        imports, submodules, frozen_env, pkg_dist_map, guarded_imports, local_ctx
    )
    pinned_manifest = [e.specifier for e in entries]
    notices = [p.detail for p in promotions if p.detail]
    return pinned_manifest, notices


def build_dependency_objects(
    imports: Any, 
    submodules: Dict[str, Set[str]], 
    frozen_env: Dict[str, str], 
    pkg_dist_map: Optional[Mapping[str, List[str]]] = None,
    guarded_imports: Optional[Set[str]] = None,
    local_ctx: Optional[localmodules.LocalModuleContext] = None
) -> Tuple[List[DependencyEntry], List[PromotionDetail]]:
    """Generates typed DependencyEntry instances in first-encountered order."""
    entries: List[DependencyEntry] = []
    promotions: List[PromotionDetail] = []
    guarded_set = guarded_imports or set()

    for imp in imports:
        if imp in STD_LIB:
            continue
        submods = submodules.get(imp, set())
        is_guarded = imp in guarded_set
        dep_entry, promo = resolve_pypi_package_and_extras(
            imp, submods, frozen_env, pkg_dist_map=pkg_dist_map, is_guarded=is_guarded, local_ctx=local_ctx
        )
        entries.append(dep_entry)
        if promo and promo not in promotions:
            promotions.append(promo)

    return entries, promotions


def resolve_opencv_variant(submodules: Optional[Set[str]] = None) -> str:
    """Determines the appropriate OpenCV package variant installed in the active environment."""
    has_contrib = any('contrib' in s.lower() or 'aruco' in s.lower() for s in submodules) if submodules else False
    try:
        res = subprocess.run([sys.executable, "-m", "pip", "list"], capture_output=True, text=True)
        installed = res.stdout.lower()
        if "opencv-contrib-python-headless" in installed:
            return "opencv-contrib-python-headless"
        elif "opencv-python-headless" in installed:
            return "opencv-python-headless"
        elif "opencv-contrib-python" in installed:
            return "opencv-contrib-python"
        elif "opencv-python" in installed:
            return "opencv-python"
    except Exception as e:  # falls back to the default variant below
        logger.debug(f"Could not inspect installed OpenCV variants: {e}", exc_info=True)
    return "opencv-contrib-python" if has_contrib else "opencv-python"


def process_package_requirements(
    pinned_list: List[str], 
    harvested_urls: Set[str],
    base_urls: Optional[Set[str]] = None,
    auxiliary_entries: Optional[List[str]] = None,
    writefile_entries: Optional[List[str]] = None
) -> Tuple[List[str], List[Tuple[str, List[str]]], List[str]]:
    """Legacy compatibility helper: correlates pinned packages with index URLs and auxiliary entries."""
    manifest_output: List[str] = []
    local_tagged_info: List[Tuple[str, List[str]]] = []
    warnings_out: List[str] = []
    
    if base_urls:
        for url in sorted(base_urls):
            manifest_output.append(f"--index-url {url}")

    extra_urls = harvested_urls - (base_urls or set())
    if extra_urls:
        for url in sorted(extra_urls):
            manifest_output.append(f"--extra-index-url {url}")

    for item in pinned_list:
        manifest_output.append(item)
        if '+' in item:
            all_urls = sorted(harvested_urls.union(base_urls or set()))
            local_tagged_info.append((item, all_urls))
            if not all_urls:
                warnings_out.append(item)

    if auxiliary_entries:
        manifest_output.extend(auxiliary_entries)

    if writefile_entries:
        manifest_output.extend(writefile_entries)
            
    return manifest_output, local_tagged_info, warnings_out


def build_dependency_entries(
    dependencies: List[DependencyEntry],
    scoped_flags: Optional[Dict[str, List[str]]] = None,
    auxiliary_entries: Optional[List[DependencyEntry]] = None,
    writefile_entries: Optional[List[DependencyEntry]] = None
) -> Tuple[List[DependencyEntry], List[Tuple[str, List[str]]], List[DiagnosticEvent]]:
    """Attaches scoped flags to DependencyEntry objects and identifies local hardware tags."""
    flags_map = scoped_flags or {}
    local_tagged_info: List[Tuple[str, List[str]]] = []
    warnings_out: List[DiagnosticEvent] = []
    all_entries: List[DependencyEntry] = []

    for dep in dependencies:
        if not dep.is_comment and dep.name:
            matched_flags: List[str] = []
            canon_name = util.canonicalize_pkg_name(dep.name)
            for candidate in (dep.name, dep.name.lower(), canon_name):
                if candidate in flags_map:
                    matched_flags = flags_map[candidate]
                    break
            if not dep.flags:
                dep.flags = matched_flags

            if '+' in dep.version:
                local_tagged_info.append((dep.specifier, dep.flags))
                if not dep.flags:
                    warnings_out.append(
                        DiagnosticEvent(
                            type="missing_hardware_index",
                            detail=f"Specific hardware build detected: `{dep.specifier}` with no download URL harvested in code cells.",
                            level="warning"
                        )
                    )

        all_entries.append(dep)

    if auxiliary_entries:
        all_entries.extend(auxiliary_entries)

    if writefile_entries:
        all_entries.extend(writefile_entries)

    return all_entries, local_tagged_info, warnings_out
