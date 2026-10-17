"""The import join: what an import loads, from the evidence of the environment and the notebook's
folders. One helper for every import a notebook makes (normal cells and %%writefile scripts) and
every entry point (file, directory, live session), so they all give the same answer.

The order follows how Python itself resolves the import from the notebook's folder: a regular
module beside the notebook comes first on sys.path, then installed distributions, then folders
reached through a path, and a namespace-only folder (a data folder with no __init__.py) only when
nothing else provides the name."""
import importlib.machinery
import importlib.metadata
import logging
import os
import sys
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from typing import FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from steady_py import localmodules, util
from steady_py.constants import IMPORT_TO_PYPI_MAP

logger = logging.getLogger("steady_py.join")


class JoinKind:
    """Join.kind values."""
    DISTRIBUTION = "distribution"  # installed distributions provide it (more than one for namespace portions)
    EDITABLE = "editable"          # an editable install provides it
    AMBIGUOUS = "ambiguous"        # several installed distributions provide the same file
    LOCAL = "local"                # a module or folder in the notebook's folder or the project root
    CUSTOM_PATH = "custom_path"    # importable here, but from a path no distribution owns
    NOT_FOUND = "not_found"        # not importable here


@dataclass(frozen=True)
class JoinContext:
    """The evidence an import is joined against: the environment and the notebook's two folders."""
    frozen_env: Mapping[str, str]                # canonical name -> pin (installed.read_installed)
    pkg_dist_map: Mapping[str, Sequence[str]]    # import name -> distributions (packages_distributions)
    editables: Mapping[str, str]                 # canonical name -> source directory (installed.read_editables)
    notebook_dir: Optional[str] = None
    root_dir: Optional[str] = None


@dataclass(frozen=True)
class Join:
    """What one top-level import loads. `distributions` holds the providers for DISTRIBUTION and
    EDITABLE, the candidates for AMBIGUOUS, and the likely PyPI name for NOT_FOUND. `anchor` is
    'notebook_dir' or 'root_dir' for LOCAL."""
    module: str
    kind: str
    distributions: Tuple[str, ...] = ()
    anchor: str = ""


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _is_namespace(spec: ModuleSpec) -> bool:
    return spec.origin is None and spec.submodule_search_locations is not None


def _spec_locations(spec: ModuleSpec) -> List[str]:
    """The files or folders a spec loads from: its origin, or a namespace package's portions."""
    if spec.origin and os.path.isabs(spec.origin):
        return [spec.origin]
    return [str(p) for p in spec.submodule_search_locations or ()]


@util.memoize_for_run
def _environment_spec(name: str) -> Optional[ModuleSpec]:
    """Where the environment loads top-level `name` from, without running any of its code: every
    import finder in turn, with the working directory left out of the path (it is the folder
    steady-py was started from, not the notebook's)."""
    cwd = _norm(os.getcwd())
    path = [p for p in sys.path if p and _norm(p) != cwd]
    for finder in sys.meta_path:
        try:
            if finder is importlib.machinery.PathFinder:
                spec = importlib.machinery.PathFinder.find_spec(name, path)
            elif hasattr(finder, "find_spec"):
                spec = finder.find_spec(name, None)
            else:
                continue
        except (ImportError, ValueError, AttributeError, TypeError, OSError) as e:
            logger.debug(f"[Join] {type(finder).__name__} failed for '{name}': {e}")
            continue
        if spec is not None:
            return spec
    return None


def _resolve_file(dotted: str) -> Optional[str]:
    """The file a dotted import loads, descending through namespace packages (P2) without running
    any code; None when it isn't importable or ends at a namespace package."""
    parts = dotted.split(".")
    spec = _environment_spec(parts[0])
    for depth in range(1, len(parts) + 1):
        if spec is None:
            return None
        if not _is_namespace(spec) or depth == len(parts):
            break
        try:
            spec = importlib.machinery.PathFinder.find_spec(".".join(parts[:depth + 1]), list(spec.submodule_search_locations or ()))
        except (ImportError, ValueError) as e:
            logger.debug(f"[Join] find_spec failed for '{dotted}': {e}")
            return None
    return _norm(spec.origin) if spec is not None and spec.origin and os.path.isabs(spec.origin) else None


@util.memoize_for_run
def _owned_files(dist_name: str) -> FrozenSet[str]:
    """The files the first installed copy of `dist_name` lists in its RECORD."""
    try:
        dist = importlib.metadata.distribution(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return frozenset()
    return frozenset(_norm(str(dist.locate_file(f))) for f in dist.files or ())


def _editable_owning(locations: Iterable[str], editables: Mapping[str, str]) -> Optional[str]:
    """The editable distribution whose source directory holds one of `locations` (the deepest
    such directory, so a project nested in another is told apart)."""
    best: Optional[Tuple[int, str]] = None
    for location in locations:
        normalized = _norm(location)
        for canon, root in editables.items():
            root_norm = _norm(root)
            if normalized == root_norm or normalized.startswith(root_norm.rstrip(os.sep) + os.sep):
                if best is None or len(root_norm) > best[0]:
                    best = (len(root_norm), canon)
    return best[1] if best else None


def _providers(module: str, dotted: Sequence[str], candidates: List[str]) -> Join:
    """Which of several distributions listing `module` actually provide the imported files: each
    dotted import is matched against the candidates' RECORDs (P2)."""
    chosen: List[str] = []
    for name in sorted(set(dotted) | {module}, key=lambda d: (d.count("."), d)):
        origin = _resolve_file(name)
        if origin is None:
            continue
        owners = [c for c in candidates if origin in _owned_files(c)]
        if len(owners) > 1:
            return Join(module, JoinKind.AMBIGUOUS, tuple(owners))
        for owner in owners:
            if owner not in chosen:
                chosen.append(owner)
    if not chosen:
        return Join(module, JoinKind.AMBIGUOUS, tuple(candidates))
    return Join(module, JoinKind.DISTRIBUTION, tuple(chosen))


def _as_editable(join: Join, ctx: JoinContext) -> Join:
    """`join`, as EDITABLE when its one provider is an editable install."""
    if join.kind == JoinKind.DISTRIBUTION and len(join.distributions) == 1 \
            and util.canonicalize_pkg_name(join.distributions[0]) in ctx.editables:
        return Join(join.module, JoinKind.EDITABLE, join.distributions)
    return join


def join_import(module: str, dotted: Sequence[str], ctx: JoinContext, opencv_variant: Optional[str] = None) -> Join:
    """What top-level `module` loads. `dotted` holds the full names the notebook imports under it
    (`google.cloud.storage`), used only to tell namespace portions apart. `opencv_variant` is
    the OpenCV distribution resolution picked for cv2, which has its own rules (K6, C1)."""
    local = localmodules.spec_in_dir(module, ctx.notebook_dir)
    environment = _environment_spec(module)
    if local is not None and not _is_namespace(local):
        same_file = environment is not None and environment.origin and local.origin \
            and _norm(environment.origin) == _norm(local.origin)
        if not same_file:  # first on sys.path in the notebook's kernel
            return Join(module, JoinKind.LOCAL, anchor="notebook_dir")

    if opencv_variant:
        return _as_editable(Join(module, JoinKind.DISTRIBUTION, (opencv_variant,)), ctx)

    candidates: List[str] = []
    for name in ctx.pkg_dist_map.get(module, ()):  # one per distribution; shadowed copies repeat it
        canon = util.canonicalize_pkg_name(name)
        if canon in ctx.frozen_env and all(util.canonicalize_pkg_name(c) != canon for c in candidates):
            candidates.append(name)
    if len(candidates) == 1:
        return _as_editable(Join(module, JoinKind.DISTRIBUTION, (candidates[0],)), ctx)
    if len(candidates) > 1:
        return _as_editable(_providers(module, dotted, candidates), ctx)

    mapped = IMPORT_TO_PYPI_MAP.get(module, module)
    if util.canonicalize_pkg_name(mapped) in ctx.frozen_env:
        return _as_editable(Join(module, JoinKind.DISTRIBUTION, (mapped,)), ctx)

    if environment is not None:
        owner = _editable_owning(_spec_locations(environment), ctx.editables)
        if owner:
            return Join(module, JoinKind.EDITABLE, (owner,))

    if local is not None:  # a namespace-only folder, which loses to any installed package (ED6)
        return Join(module, JoinKind.LOCAL, anchor="notebook_dir")
    if ctx.root_dir and _norm(ctx.root_dir) != _norm(ctx.notebook_dir or "") \
            and localmodules.spec_in_dir(module, ctx.root_dir) is not None:
        return Join(module, JoinKind.LOCAL, anchor="root_dir")
    if environment is not None:
        return Join(module, JoinKind.CUSTOM_PATH)
    return Join(module, JoinKind.NOT_FOUND, (mapped,))
