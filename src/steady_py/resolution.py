"""Turning what a notebook imports and installs into dependency entries: the unified install/import
timeline, import-to-distribution resolution, and the pinned entries the manifest is built from."""
import importlib.metadata
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from steady_py import installed, join, magics, pypi, scanning, util
from steady_py.constants import BUILD_AND_PACKAGING_TOOLS, DependencyStatus, PLATFORM_PSEUDO_MODULES, Provenance, STD_LIB
from steady_py.models import DependencyEntry, DiagnosticEvent, ImportOccurrence, PromotionDetail, TimelineResult

logger = logging.getLogger("steady_py.resolution")


# =====================================================================
# UNIFIED TIMELINE ENGINE
# =====================================================================

def _skipped_import(module: str) -> bool:
    """Imports that are never a dependency entry of their own: the standard library."""
    return module.lower() in STD_LIB


def _join(module: str, dotted: Set[str], ctx: join.JoinContext) -> join.Join:
    """The import join for `module`, with OpenCV's variant rule applied for cv2 (K6, C1)."""
    variant = resolve_opencv_variant(dotted, dict(ctx.frozen_env)) if module == "cv2" else None
    return join.join_import(module, sorted(dotted), ctx, opencv_variant=variant)


def _join_key(result: join.Join) -> List[str]:
    """The timeline keys an import occupies: one per providing distribution, so an install line and
    an import of one distribution give one entry whatever their names (G2); the likely PyPI name
    when it isn't installed; the module name otherwise."""
    if result.kind in (join.JoinKind.DISTRIBUTION, join.JoinKind.EDITABLE, join.JoinKind.NOT_FOUND):
        return [util.canonicalize_pkg_name(d) for d in result.distributions]
    return [f"import:{result.module}"]


def build_unified_timeline(
    code_sources: scanning.CellsLike,
    ctx: join.JoinContext,
    is_execution_ordered: bool = True,
) -> TimelineResult:
    """
    Constructs the master sequence of DependencyEntry objects:
    - Explicit pip install occurrences anchor timeline coordinates.
    - Imports anchor a position only for what no install line already placed. Every import goes
      through the import join, so pip and import names meet on the distribution (G2).
    - An unconditional install line wins over a guarded import (decision 2); otherwise an entry
      is guarded only when every import that reaches it is.
    """
    pip_occs, _raw_installs = magics.harvest_pip_install_occurrences(code_sources)
    resolved_pips, conflict_warnings = magics.resolve_pip_occurrences(pip_occs, is_execution_ordered=is_execution_ordered)

    occurrences: List[ImportOccurrence] = []
    for cell_idx, cell in enumerate(scanning.as_cells(code_sources)):
        occurrences.extend(scanning.extract_import_occurrences_from_source(cell.source, cell_idx=cell_idx, cell=cell))

    dotted: Dict[str, Set[str]] = {}
    for occ in occurrences:
        if not _skipped_import(occ.module):
            dotted.setdefault(occ.module, set()).update(occ.dotted_names)
    joins = {module: _join(module, names, ctx) for module, names in dotted.items()
             if module not in PLATFORM_PSEUDO_MODULES and module not in BUILD_AND_PACKAGING_TOOLS}

    events: List[Tuple[Tuple[int, int], str, str]] = []  # (coord, kind, key)
    first_import: Dict[str, ImportOccurrence] = {}
    submods: Dict[str, Set[str]] = {}
    unguarded: Set[str] = set()

    for norm_key, pip_occ in resolved_pips.items():
        canon = util.canonicalize_pkg_name(pip_occ.name)
        if norm_key == canon:
            events.append(((pip_occ.cell_idx, pip_occ.line_idx), "PIP", canon))
            unguarded.add(canon)

    placed = {key for _, _, key in events}
    for occ in occurrences:
        if occ.module not in dotted:
            continue
        keys = [f"import:{occ.module}"] if occ.module not in joins else _join_key(joins[occ.module])
        for key in keys:
            first_import.setdefault(key, occ)
            if key not in placed:
                placed.add(key)
                events.append(((occ.cell_idx, occ.line_idx), "IMPORT", key))
            if occ.full_name and "." in occ.full_name:
                submods.setdefault(key, set()).add(occ.full_name)
            if not occ.is_guarded:
                unguarded.add(key)

    events.sort(key=lambda t: t[0])

    result = TimelineResult(conflict_warnings=conflict_warnings)
    for _coord, kind, key in events:
        guarded = key not in unguarded
        occ_import = first_import.get(key)
        if kind == "PIP":
            pip = resolved_pips[key]
            imported_as = occ_import.module if occ_import else None
            resolved = _distribution(pip.name, submods.get(key, set()), ctx, guarded=False,
                                     imported_as=imported_as, where=occ_import, placed=placed)
            entry = resolved.entries[0]
            entry.source = "pip_command"
            if pip.version_spec and not entry.is_comment:
                version, from_line, warning = line_pin(pip.name, pip.version_spec, entry.version or "")
                entry.version = version
                if from_line:
                    entry.version_source = Provenance.NOTEBOOK
                if warning:
                    resolved.warnings.append(DiagnosticEvent.at(pip.cell, pip.line_idx, "install_line_version", warning))
            if pip.extras and not entry.is_comment:
                base, _extra = installed.split_pin_name(entry.name)
                entry.name = f"{base}[{','.join(pip.extras)}]"  # G16: the install line's extras, as written
            entry.flags = list(pip.flags)
        else:
            assert occ_import is not None
            module = occ_import.module
            target = joins.get(module) or join.Join(module, join.JoinKind.NOT_FOUND)
            resolved = _for_import(module, target, key, submods.get(key, set()), ctx, guarded, occ_import, placed)
        result.add(resolved)
    return result


_EXACT = re.compile(r"^\s*===?\s*([^\s,*]+)\s*$")


def line_pin(name: str, spec: str, installed_version: str) -> Tuple[str, bool, Optional[str]]:
    """The version to pin for an installed package that an install line names with `spec`, whether
    it came from the line, and a warning when the line and the installed version disagree (P1).

    An explicit `==` is kept as written, since the line installs that version after Cell 2 anyway.
    Any other spec pins what is installed, the version the notebook ran with.
    """
    exact = _EXACT.match(spec)
    if exact:
        version = exact.group(1)
        if version != installed_version and not _same_version(version, installed_version):
            return version, True, (f"The notebook installs {name}=={version}, but {installed_version} is installed here. "
                                   f"Cell 2 pins {version}, since that line installs it after Cell 2 anyway.")
        return version, True, None
    try:
        allowed = SpecifierSet(spec).contains(installed_version, prereleases=True)
    except (InvalidSpecifier, InvalidVersion):
        allowed = False
    if allowed:
        return installed_version, False, None
    return installed_version, False, (f"The installed {name} {installed_version} doesn't match '{name}{spec}' in the notebook. "
                                      f"Cell 2 pins {installed_version}, the version this notebook ran with; check the line.")


def _same_version(a: str, b: str) -> bool:
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return False


# =====================================================================
# ENVIRONMENT CORRELATION & EXTRAS PROMOTION
# =====================================================================

@dataclass
class Resolved:
    """The entries one import or install line produces, with what to tell the creator about it."""
    entries: List[DependencyEntry] = field(default_factory=list)
    promotion: Optional[PromotionDetail] = None
    warnings: List[DiagnosticEvent] = field(default_factory=list)
    notices: List[DiagnosticEvent] = field(default_factory=list)


def _at(where: Optional[ImportOccurrence], type: str, detail: str, level: str = "warning") -> DiagnosticEvent:
    """A diagnostic at the import that raised it, or unlocated (a %%writefile script's import)."""
    if where is None:
        return DiagnosticEvent(type=type, detail=detail, level=level)
    return DiagnosticEvent.at(where.cell, where.line_idx, type, detail, level=level)


def _for_import(
    module: str, target: join.Join, key: str, submodules: Set[str], ctx: join.JoinContext, guarded: bool,
    where: Optional[ImportOccurrence], placed: Set[str],
) -> Resolved:
    """The entries for one import, by what the join found it loads."""
    if module in PLATFORM_PSEUDO_MODULES:
        return Resolved([DependencyEntry(
            name=module, status=DependencyStatus.PLATFORM_PSEUDO_MODULE, is_comment=True,
            comment_text=f"# {module} (provided automatically by platform like Colab/Databricks; no install needed)")])
    if module in BUILD_AND_PACKAGING_TOOLS:
        return Resolved([DependencyEntry(
            name=module, status=DependencyStatus.BUILD_TOOL, is_comment=True,
            comment_text=f"# {module} (core Python build/packaging tool; excluded from requirement lockfiles)")])
    if target.kind == join.JoinKind.LOCAL:
        return Resolved([DependencyEntry(
            name=module, status=DependencyStatus.LOCAL_MODULE, is_comment=True,
            comment_text=f"# {module} (local folder/file next to notebook; ensure sibling files were shared)",
            anchor=target.anchor)])
    if target.kind == join.JoinKind.CUSTOM_PATH:
        return Resolved(
            [DependencyEntry(name=module, status=DependencyStatus.CUSTOM_PATH, is_comment=True,
                             comment_text=f"# {module} (importable on this machine from a folder no installed package owns; can't be pinned)")],
            warnings=[_at(where, "custom_path_import",
                          f"'{module}' is importable here from a folder that no installed package owns (one added with "
                          f"PYTHONPATH, a .pth file or `conda develop`, or a package installed without pip's records), "
                          f"so it can't be pinned. Make sure anyone running this notebook has it.")])
    if target.kind == join.JoinKind.AMBIGUOUS:
        names = ", ".join(target.distributions)
        return Resolved(
            [DependencyEntry(name=module, status=DependencyStatus.AMBIGUOUS, is_comment=True,
                             comment_text=f"# {module} (provided by more than one installed package: {names}; none pinned)")],
            warnings=[_at(where, "ambiguous_import",
                          f"'{module}' is provided by more than one installed package ({names}), so steady-py can't "
                          f"tell which one the notebook uses and pins none of them.")])
    dist = next(d for d in target.distributions if util.canonicalize_pkg_name(d) == key)
    return _distribution(dist, submodules, ctx, guarded=guarded, imported_as=module, where=where, placed=placed)


def _distribution(
    dist: str, submodules: Set[str], ctx: join.JoinContext, guarded: bool, imported_as: Optional[str],
    where: Optional[ImportOccurrence], placed: Set[str],
) -> Resolved:
    """The entries for one distribution, by how it is installed: not at all, editable, from a
    local file or a URL, or from an index (pinned, with extras promotion)."""
    label = imported_as or dist
    canon = util.canonicalize_pkg_name(dist)
    if canon in BUILD_AND_PACKAGING_TOOLS:  # `%pip install -U pip` too: the runner's tools are its own
        return Resolved([DependencyEntry(
            name=dist, status=DependencyStatus.BUILD_TOOL, is_comment=True,
            comment_text=f"# {dist} (core Python build/packaging tool; excluded from requirement lockfiles)")])
    matched_pin = ctx.frozen_env.get(canon)
    if canon in ctx.editables:
        return _editable(dist, label, ctx, guarded, where, placed)

    pin_version: Optional[str] = None
    direct_url: Optional[str] = None
    if matched_pin:
        _, pin_version, direct_url = installed.split_frozen_pin(matched_pin)
        if direct_url and installed.is_local_direct_url(direct_url) and _installed_by_conda(dist):
            return _conda_built(dist, label, guarded, where)

    if guarded:
        if direct_url:
            return Resolved([DependencyEntry(
                name=dist, version="", status=DependencyStatus.GUARDED, is_comment=True,
                comment_text=f"# {dist} (optional or conditional dependency inside try/except block; {installed.direct_reference_note(direct_url)})")],
                warnings=_local_install_warning(dist, label, direct_url, where, ctx))
        if matched_pin:
            return Resolved([DependencyEntry(
                name=dist, version=pin_version or "", status=DependencyStatus.GUARDED, is_comment=True,
                comment_text=f"# {matched_pin} (optional or conditional dependency inside try/except block)")])
        return Resolved([DependencyEntry(
            name=dist, version="", status=DependencyStatus.GUARDED, is_comment=True,
            comment_text=f"# {dist} (optional or conditional dependency inside try/except block)")])

    if not matched_pin:
        return Resolved([DependencyEntry(
            name=dist, version="", status=DependencyStatus.PINNED, is_comment=True,
            comment_text=f"# {dist} (imported as '{label}'; not installed here or found as a local file -- verify before assuming this is truly missing)")])

    if direct_url:
        note = installed.direct_reference_note(direct_url, cell2_installs=True)
        if installed.is_local_direct_url(direct_url):
            return Resolved([DependencyEntry(
                name=dist, status=DependencyStatus.SYSTEM_PATH, is_comment=True,
                comment_text=f"# {dist} (imported as '{label}'; {note})")],
                warnings=_local_install_warning(dist, label, direct_url, where, ctx))
        return Resolved([DependencyEntry(
            name=dist, status=DependencyStatus.DIRECT_REFERENCE, is_comment=True,
            comment_text=f"# {dist} (imported as '{label}'; {note})", direct_url=direct_url)])

    pkg_part, ver, _ = installed.split_frozen_pin(matched_pin)
    ver_part = conda_opencv_release(pkg_part, ver or "") or ver or ""
    promoted = _promoted_extra(pkg_part, submodules)
    if promoted:
        promoted_name = f"{pkg_part}[{promoted}]"
        promoted_pin = f"{promoted_name}=={ver_part}"
        promo = PromotionDetail(
            import_name=f"{label}.{promoted}", promoted_name=promoted_name, version=ver_part,
            detail=f"💡 Extra Dependency Promotion: importing '{label}.{promoted}' automatically promoted requirement to '{promoted_pin}'")
        return Resolved([DependencyEntry(name=promoted_name, version=ver_part, status=DependencyStatus.PINNED)], promotion=promo)
    return Resolved([DependencyEntry(name=pkg_part, version=ver_part, status=DependencyStatus.PINNED)])


def _promoted_extra(dist: str, submodules: Set[str]) -> Optional[str]:
    """The extra whose name matches an imported submodule's last part, if any (K9 tracks this
    heuristic's limits)."""
    if not submodules:
        return None
    try:
        extras: List[str] = importlib.metadata.distribution(dist).metadata.get_all("Provides-Extra") or []
    except importlib.metadata.PackageNotFoundError:
        return None  # not installed here, so there are no extras to match
    provided = {e.lower(): e for e in extras}
    for sub in submodules:
        tail = sub.split('.')[-1].lower()
        if tail in provided:
            return provided[tail]
    return None


def _local_install_warning(dist: str, label: str, direct_url: str, where: Optional[ImportOccurrence],
                           ctx: join.JoinContext) -> List[DiagnosticEvent]:
    """The needs-attention warning for a package installed from a file on this machine (E2a, ED3).
    None when the notebook's own install line installs that file: it is carried in raw_installs,
    and the install line already has its notice."""
    if not installed.is_local_direct_url(direct_url):
        return []
    if os.path.normcase(installed.file_url_path(direct_url)) in ctx.local_installs:
        return []
    return [_at(where, "local_file_import",
                f"'{label}' comes from {dist}, installed from a file on this machine, so anyone running this "
                f"notebook can't install it from there. Publish it, or host it at a shared URL.")]


def _conda_built(dist: str, label: str, guarded: bool, where: Optional[ImportOccurrence]) -> Resolved:
    """A conda-built package (its direct_url.json points at conda's build folder): pinned as
    name==version from PyPI, with a notice, since conda and PyPI builds can differ (E2b)."""
    version = importlib.metadata.distribution(dist).version
    if guarded:
        return Resolved([DependencyEntry(
            name=dist, version=version, status=DependencyStatus.GUARDED, is_comment=True,
            comment_text=f"# {dist}=={version} (optional or conditional dependency inside try/except block)")])
    notice = _at(where, "conda_package",
                 f"{dist} {version} was built and installed by conda; pinned {dist}=={version} from PyPI, "
                 f"whose build can differ from conda's.", level="notice")
    return Resolved([DependencyEntry(name=dist, version=version, status=DependencyStatus.PINNED)], notices=[notice])


def _editable(dist: str, label: str, ctx: join.JoinContext, guarded: bool, where: Optional[ImportOccurrence],
              placed: Set[str]) -> Resolved:
    """An editable install: a comment and a needs-attention warning (ED3), since runners can't
    install it, plus a pin for each of its own requirements that nothing else in the notebook
    places (ED1), as installed here. The `editables` shim hatchling and pdm inject is left out."""
    resolved = Resolved(
        [DependencyEntry(name=dist, status=DependencyStatus.EDITABLE, is_comment=True,
                         comment_text=f"# {dist} (imported as '{label}'; your in-development package, installed "
                                      f"in editable mode; runners can't install it)")],
        warnings=[_editable_warning(dist, label, ctx, where)])
    for requirement in _editable_requirements(dist):
        canon = util.canonicalize_pkg_name(requirement)
        if canon in placed:
            continue
        placed.add(canon)
        dependency = _distribution(requirement, set(), ctx, guarded=guarded, imported_as=None, where=where, placed=placed)
        for entry in dependency.entries:
            entry.source = "editable_requirement"
        resolved.entries.extend(dependency.entries)
        resolved.warnings.extend(dependency.warnings)
        resolved.notices.extend(dependency.notices)
    return resolved


def _editable_warning(dist: str, label: str, ctx: join.JoinContext, where: Optional[ImportOccurrence]) -> DiagnosticEvent:
    """The needs-attention warning for an import an editable install provides (ED3), naming the Git
    source that could replace it when the folder is a checkout with a remote (ED5)."""
    candidate = _git_candidate(ctx.editables[util.canonicalize_pkg_name(dist)])
    detail = (f"'{label}' comes from {dist}, your own package installed in editable mode from a folder on this "
              f"machine, so anyone running this notebook can't install it. Publish it, or share it from a Git URL")
    detail += (f": `{candidate}` would install this commit, once it is pushed and the folder has no uncommitted "
               f"changes." if candidate else ".")
    return _at(where, "editable_import", detail)


def _editable_requirements(dist: str) -> List[str]:
    """The names of an editable's own requirements that apply here, in METADATA order. Markers are
    evaluated for this machine with no extras, as pip does for a plain install."""
    try:
        requires = importlib.metadata.distribution(dist).requires or []
    except importlib.metadata.PackageNotFoundError:
        return []
    names: List[str] = []
    for line in requires:
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            logger.debug(f"Unreadable requirement '{line}' of {dist}")
            continue
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        if util.canonicalize_pkg_name(requirement.name) != "editables" and requirement.name not in names:
            names.append(requirement.name)
    return names


@util.memoize_for_run
def _git_candidate(folder: str) -> Optional[str]:
    """`git+<remote>@<commit>` (with `#subdirectory=` when the project isn't the repository's root)
    for an editable's folder, read with the git command line; None when it isn't a git checkout
    with a remote, or git isn't available (ED5)."""
    def git(*args: str) -> Optional[str]:
        try:
            done = subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug(f"[Join] git {' '.join(args)} failed in {folder}: {e}")
            return None
        return done.stdout.strip() or None if done.returncode == 0 else None
    remote, commit, top = git("config", "--get", "remote.origin.url"), git("rev-parse", "HEAD"), git("rev-parse", "--show-toplevel")
    if not (remote and commit and top):
        return None
    spec = f"git+{remote}@{commit}"
    subdirectory = os.path.relpath(os.path.realpath(folder), os.path.realpath(top)).replace(os.sep, "/")
    return spec + (f"#subdirectory={subdirectory}" if subdirectory != "." else "")


def build_auxiliary_tool_entries(
    harvested_packages: Set[str],
    imported_packages: Any,
    frozen_env: Mapping[str, str]
) -> List[DependencyEntry]:
    """Builds commented DependencyEntry instances for CLI tools installed via cell magics. These are
    distribution names from install lines, so they are looked up by name, not joined as imports."""
    aux_entries: List[DependencyEntry] = []
    imported_set = {util.canonicalize_pkg_name(imp) for imp in imported_packages}
    unimported_tools = sorted([
        pkg for pkg in harvested_packages 
        if util.canonicalize_pkg_name(pkg) not in imported_set and pkg.lower() not in STD_LIB
    ])

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
    ctx: join.JoinContext,
) -> Resolved:
    """Commented entries for imports found only inside %%writefile scripts, joined like any other
    import (ED4), with the same warnings for what runners can't install."""
    resolved = Resolved()
    primary_set = {util.canonicalize_pkg_name(imp) for imp in primary_imports}
    script_only = sorted([
        pkg for pkg in writefile_imports 
        if util.canonicalize_pkg_name(pkg) not in primary_set and not _skipped_import(pkg)
    ])

    for module in script_only:
        target = _join(module, {module}, ctx)
        where = "imported inside script generated via %%writefile"
        name = target.distributions[0] if target.distributions else module
        matched_pin = ctx.frozen_env.get(util.canonicalize_pkg_name(name)) if target.distributions else None
        text = f"# {name}  ({where}; not found in active env)"
        version = ""
        if target.kind == join.JoinKind.LOCAL:
            text = f"# {module}  ({where}; local folder/file next to notebook)"
        elif target.kind == join.JoinKind.EDITABLE:
            text = f"# {name}  ({where}; your in-development package, installed in editable mode)"
            resolved.warnings.append(_editable_warning(name, module, ctx, None))
        elif target.kind in (join.JoinKind.CUSTOM_PATH, join.JoinKind.AMBIGUOUS):
            text = f"# {module}  ({where}; can't be pinned, see the warning)"
            resolved.warnings.extend(_for_import(module, target, "", set(), ctx, False, None, set()).warnings)
        elif matched_pin:
            _, pin_version, direct_url = installed.split_frozen_pin(matched_pin)
            if direct_url:
                text = f"# {name}  ({where}; {installed.direct_reference_note(direct_url)})"
                resolved.warnings.extend(_local_install_warning(name, module, direct_url, None, ctx))
            else:
                text, version = f"# {matched_pin}  ({where})", pin_version or ""
        resolved.entries.append(DependencyEntry(
            name=name, version=version, source="writefile_script", status=DependencyStatus.WRITEFILE_SCRIPT,
            is_comment=True, comment_text=text))
    return resolved


def build_manifest_entries(
    imports: Any, 
    submodules: Dict[str, Set[str]], 
    ctx: join.JoinContext,
    guarded_imports: Optional[Set[str]] = None,
) -> Tuple[List[str], List[str]]:
    """Builds string-formatted manifest lines for legacy/batch consumers while preserving order."""
    entries, promotions = build_dependency_objects(imports, submodules, ctx, guarded_imports)
    pinned_manifest = [e.specifier for e in entries]
    notices = [p.detail for p in promotions if p.detail]
    return pinned_manifest, notices


def build_dependency_objects(
    imports: Any, 
    submodules: Dict[str, Set[str]], 
    ctx: join.JoinContext,
    guarded_imports: Optional[Set[str]] = None,
) -> Tuple[List[DependencyEntry], List[PromotionDetail]]:
    """Generates typed DependencyEntry instances in first-encountered order (`--universal`)."""
    entries: List[DependencyEntry] = []
    promotions: List[PromotionDetail] = []
    guarded_set = guarded_imports or set()
    placed: Set[str] = set()

    for imp in imports:
        if _skipped_import(imp):
            continue
        submods = submodules.get(imp, set())
        target = _join(imp, submods | {imp}, ctx)
        for key in _join_key(target):
            if key in placed:
                continue
            placed.add(key)
            resolved = _for_import(imp, target, key, submods, ctx, imp in guarded_set, None, placed)
            entries.extend(resolved.entries)
            if resolved.promotion and resolved.promotion not in promotions:
                promotions.append(resolved.promotion)

    return entries, promotions


_OPENCV_VARIANTS = ("opencv-python", "opencv-python-headless", "opencv-contrib-python", "opencv-contrib-python-headless")


def _installed_by_conda(name: str) -> bool:
    try:
        return (importlib.metadata.distribution(name).read_text("INSTALLER") or "").strip() == "conda"
    except importlib.metadata.PackageNotFoundError:
        return False


def resolve_opencv_variant(submodules: Optional[Set[str]] = None, frozen_env: Optional[Mapping[str, str]] = None) -> str:
    """The installed OpenCV variant that `import cv2` loads: the first on sys.path, since
    `frozen_env` keeps installed distributions in sys.path order. conda-forge writes stubs for
    both opencv-python and its headless twin in every build, so with conda stubs alone the
    GUI variant is chosen, a superset of the headless one (C1). With none installed, the
    contrib variant when the notebook uses a contrib submodule, else the base one."""
    found = [canon for canon in frozen_env or {} if canon in _OPENCV_VARIANTS]
    if found and "opencv-python" in found and all(_installed_by_conda(canon) for canon in found):
        return "opencv-python"
    if found:
        return found[0]
    has_contrib = any('contrib' in s.lower() or 'aruco' in s.lower() for s in submodules) if submodules else False
    return "opencv-contrib-python" if has_contrib else "opencv-python"


def conda_opencv_release(name: str, version: str) -> Optional[str]:
    """The PyPI release for a conda-forge OpenCV stub (C1): conda versions have three parts
    (5.0.0), PyPI's four (5.0.0.93), so `==5.0.0` matches nothing. Returns the newest PyPI
    release with the same first three parts, or None when `name` isn't a conda-built OpenCV
    or PyPI has no such release."""
    if util.canonicalize_pkg_name(name) not in _OPENCV_VARIANTS or not _installed_by_conda(name):
        return None
    try:
        conda_version = Version(version)
    except InvalidVersion:
        return None
    if len(conda_version.release) > 3:
        return None
    matching = []
    for release in pypi.fetch_pypi_package_metadata(name).releases or {}:
        try:
            candidate = Version(release)
        except InvalidVersion:
            continue
        if candidate.release[:3] == conda_version.release[:3] and not candidate.is_prerelease:
            matching.append(candidate)
    return str(max(matching)) if matching else None


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
