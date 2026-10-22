"""What is installed in the running interpreter: the pin for each distribution, including
packages installed from a URL, a local path or an editable checkout (direct references)."""
import importlib.metadata
import json
import logging
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request
from typing import Dict, FrozenSet, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

from steady_py import util

logger = logging.getLogger("steady_py.installed")


_VCS_URL_PREFIXES = ("git+", "hg+", "svn+", "bzr+")


def split_frozen_pin(pin: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Splits one frozen-environment entry into (name, version, direct_url).

    'name==1.2' -> (name, '1.2', None). 'name @ url' (a package installed from a
    direct reference, not PyPI) -> (name, None, url). Every consumer of a frozen
    entry goes through this, so none of them can assume the '==' shape.
    """
    if " @ " in pin:
        name, url = pin.split(" @ ", 1)
        return name.strip(), None, url.strip()
    if "===" in pin:  # a version that isn't PEP 440, kept verbatim
        name, version = pin.split("===", 1)
        return name.strip(), version.strip(), None
    if "==" in pin:
        name, version = pin.split("==", 1)
        return name.strip(), version.strip(), None
    return pin.strip(), None, None


def split_pin_extras(name: str) -> Tuple[str, FrozenSet[str]]:
    """Splits a pin name into (bare PyPI project name, every requested extra).

    Pin names can carry an extras tag (e.g. "pandas[test]") from extras
    promotion elsewhere in this tool. PyPI's JSON API only resolves bare
    project names -- passing the extras-tagged form straight through
    404s and gets misread as "removed from PyPI entirely."
    """
    try:
        req = Requirement(name)
        return req.name, frozenset(req.extras)
    except InvalidRequirement:
        return name, frozenset()


def split_pin_name(name: str) -> Tuple[str, Optional[str]]:
    """Like split_pin_extras, for callers that only need the bare name.
    The second value is the alphabetically first extra (deterministic), or None."""
    bare, extras = split_pin_extras(name)
    return bare, (min(extras) if extras else None)


def has_local_version_identifier(version: str) -> bool:
    """True if this pin has a PEP 440 local version segment (e.g. '2.3.1+cu121').

    PyPI's own upload policy rejects any package with a local version label --
    a public index can never host one. So a '+'-tagged pin will always 404
    against pypi.org regardless of which index it actually came from (a custom
    wheel index like download.pytorch.org, a private mirror, etc). Checking
    this is more robust than parsing --index-url/--extra-index-url flags: it's
    a direct, standards-based guarantee, not an inference from how the pin
    happened to be installed.
    """
    try:
        return Version(version).local is not None
    except InvalidVersion:
        return False


def _strip_vcs_prefix(spec: str) -> str:
    lowered = spec.lower()
    for prefix in _VCS_URL_PREFIXES:
        if lowered.startswith(prefix):
            return spec[len(prefix):]
    return spec


def is_local_direct_url(url: str) -> bool:
    """True for a direct reference that lives on this machine (file:// or git+file://)."""
    return _strip_vcs_prefix(url).lower().startswith("file:")


_NAMED_REFERENCE = re.compile(r"^[A-Za-z0-9._-]+(?:\[[^\]]*\])?\s*@\s*(\S+)")


def _direct_source_key(spec: str) -> Tuple[str, str, str]:
    """Host, path and subdirectory of a direct-reference spec, ignoring a `name @` lead (CH3), VCS
    prefix, @ref, .git and every fragment but `subdirectory=`, which tells a monorepo's packages
    apart (E5)."""
    spec = spec.strip().strip("'\"")
    named = _NAMED_REFERENCE.match(spec)
    parts = urllib.parse.urlsplit(_strip_vcs_prefix(named.group(1) if named else spec))
    path = parts.path
    head, sep, tail = path.rpartition("@")
    if sep and "/" not in tail:  # a trailing @ref; user@host lives in netloc, not here
        path = head
    path = path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    subdirectory = urllib.parse.parse_qs(parts.fragment).get("subdirectory", [""])[0].strip("/")
    return parts.netloc.lower(), path.lower(), subdirectory


def same_direct_source(a: str, b: str) -> bool:
    """True if two direct-reference specs point at the same repository/archive, whatever ref each pins."""
    return _direct_source_key(a) == _direct_source_key(b)


def direct_reference_note(direct_url: str, cell2_installs: bool = False) -> str:
    """Plain-language description of where a non-PyPI package came from, for generated comments.
    Says Cell 2 installs it only when `cell2_installs` (E8). Never includes the URL or path itself."""
    if is_local_direct_url(direct_url):
        return ("found on a system-dependent path, which can't and shouldn't be shared directly. "
                "Publish it or host it at a shared URL so others can install it")
    if cell2_installs:
        return ("installed from a direct URL, not PyPI; it is installed from the non-standard sources "
                "list, so make sure anyone running this notebook can reach that source")
    return ("installed from a direct URL, not PyPI, so make sure anyone running this notebook can "
            "reach that source")


def _direct_reference(info: object, name: str) -> Optional[str]:
    """'name @ url' for one parsed direct_url.json, written as pip's
    direct_url_as_pep440_direct_reference writes it (VCS commit, the first archive hash,
    subdirectory), or None when the record is unusable."""
    if not isinstance(info, dict) or not isinstance(info.get("url"), str):
        return None
    url, fragments = info["url"], []
    vcs_info, archive_info = info.get("vcs_info"), info.get("archive_info")
    if isinstance(vcs_info, dict) and vcs_info.get("vcs"):
        url = f"{vcs_info['vcs']}+{url}@{vcs_info.get('commit_id', '')}"
    elif isinstance(archive_info, dict):
        hashes = archive_info.get("hashes")
        if isinstance(hashes, dict) and hashes:
            fragments.append("=".join(next(iter(hashes.items()))))
        elif isinstance(archive_info.get("hash"), str):  # the legacy single-hash field
            fragments.append(archive_info["hash"])
    if isinstance(info.get("subdirectory"), str) and info["subdirectory"]:
        fragments.append("subdirectory=" + info["subdirectory"])
    return f"{name} @ {url}" + ("#" + "&".join(fragments) if fragments else "")


def self_pin(version: str) -> str:
    """How Cell 2 installs steady-py: the way this copy was installed when anyone running the
    notebook can reach that source (a remote VCS commit or archive), else `steady-py==version`,
    the index release (a local or editable copy can't be shared)."""
    try:
        dist = importlib.metadata.distribution("steady-py")
    except importlib.metadata.PackageNotFoundError:
        return f"steady-py=={version}"
    info = _direct_url_info(dist)
    url = info.get("url") if info else None
    if isinstance(url, str) and not is_local_direct_url(url):
        reference = _direct_reference(info, "steady-py")
        if reference:
            return reference
    return f"steady-py=={version}"


def _egg_link_location(name: str) -> Optional[str]:
    """The source directory of a legacy `setup.py develop` install, from its .egg-link file."""
    stems = {name, re.sub(r"[-_.]+", "-", name), re.sub(r"[-_.]+", "_", name)}
    for entry in sys.path:
        for stem in stems:
            link = os.path.join(entry or ".", f"{stem}.egg-link")
            if os.path.isfile(link):
                with open(link, encoding="utf-8", errors="replace") as f:
                    return f.readline().strip() or None
    return None


def _first_copies() -> Iterator[Tuple[str, importlib.metadata.Distribution]]:
    """(canonical name, distribution) for every installed distribution, the first copy on
    sys.path only, as at import."""
    seen: Set[str] = set()
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if not name or dist.version is None:
            continue
        canon = util.canonicalize_pkg_name(name)
        if canon not in seen:
            seen.add(canon)
            yield canon, dist


def _direct_url_info(dist: importlib.metadata.Distribution) -> Optional[Dict[str, object]]:
    """The parsed direct_url.json of `dist`, or None when it has none or it can't be read."""
    try:
        raw = dist.read_text("direct_url.json")
        info = json.loads(raw) if raw else None
    except (OSError, ValueError) as e:  # JSONDecodeError is a ValueError
        logger.debug(f"Unreadable direct_url.json for {dist.metadata['Name']}: {e}")
        return None
    return info if isinstance(info, dict) else None


def read_installed() -> Dict[str, str]:
    """Every distribution the running interpreter can import, read from importlib.metadata:
    canonical name -> pin. The first copy on sys.path wins, as at import. Ordinary installs
    pin 'name==version' ('name===version' when the version isn't PEP 440); direct references
    pin 'name @ url' from their direct_url.json; legacy develop installs pin their source
    directory. pip freeze agrees apart from what it hides (pip, and setuptools and wheel before
    Python 3.12) and editables, which it prints as `-e` lines; tests use it as the oracle."""
    pins: Dict[str, str] = {}
    for canon, dist in _first_copies():
        name, version = dist.metadata["Name"], dist.version
        info = _direct_url_info(dist)
        pin = _direct_reference(info, name) if info else None
        if pin is None:
            location = _egg_link_location(name)
            if location:
                pin = f"{name} @ {pathlib.Path(location).resolve().as_uri()}"
        if pin is None:
            try:
                Version(version)
                pin = f"{name}=={version}"
            except InvalidVersion:
                pin = f"{name}==={version}"
        pins[canon] = pin
    return pins


def read_editables() -> Dict[str, str]:
    """canonical name -> source directory for every editable install, the first copy on sys.path
    only: PEP 660 installs from direct_url.json's `dir_info.editable`, legacy `setup.py develop`
    installs from their .egg-link. The import join matches module origins against these."""
    roots: Dict[str, str] = {}
    for canon, dist in _first_copies():
        info = _direct_url_info(dist)
        dir_info = info.get("dir_info") if info else None
        url = info.get("url") if info else None
        if isinstance(dir_info, dict) and dir_info.get("editable") and isinstance(url, str) and url.startswith("file:"):
            roots[canon] = file_url_path(url)
        elif info is None:
            location = _egg_link_location(dist.metadata["Name"])
            if location:
                roots[canon] = str(pathlib.Path(location).resolve())
    return roots


def local_paths(specs: Sequence[str], base_dir: str) -> Tuple[str, ...]:
    """The normalized local paths among install-line specs (`./x.whl`, `/abs/dir`, `file://...`),
    relative ones taken from `base_dir`; URLs, VCS sources and `name @ url` specs are left out."""
    paths: List[str] = []
    for spec in specs:
        if spec.startswith("file:"):
            paths.append(os.path.normcase(file_url_path(spec)))
        elif "://" not in spec and "@" not in spec and not spec.startswith(("git+", "hg+", "svn+", "bzr+")):
            path = pathlib.Path(spec)
            paths.append(os.path.normcase(str((path if path.is_absolute() else pathlib.Path(base_dir) / path).resolve())))
    return tuple(paths)


def file_url_path(url: str) -> str:
    """The local path of a file:// URL, as the OS writes it."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.request.url2pathname(urllib.parse.unquote(parts.path))
    if parts.netloc and parts.netloc != "localhost":  # a UNC share
        path = f"//{parts.netloc}{path}"
    return str(pathlib.Path(path).resolve())


def reaching(roots: Mapping[str, Sequence[str]], targets: Set[str]) -> Dict[str, str]:
    """For each canonical name in `targets` that installing `roots` would pull in, the root whose
    installed dependencies reach it. `roots` maps a canonical package to the extras it is pinned with.
    Dependencies are read from what is installed here, with markers evaluated for this environment."""
    found: Dict[str, str] = {}
    for root, extras in roots.items():
        seen: Set[str] = {root}
        queue: List[Tuple[str, Sequence[str]]] = [(root, extras)]
        while queue:
            name, wanted = queue.pop()
            try:
                requires = importlib.metadata.requires(name) or []
            except importlib.metadata.PackageNotFoundError:
                continue
            for line in requires:
                try:
                    req = Requirement(line)
                except InvalidRequirement:
                    continue
                if req.marker is not None and not any(req.marker.evaluate({"extra": e}) for e in ("", *wanted)):
                    continue
                dep = util.canonicalize_pkg_name(req.name)
                if dep in seen:
                    continue
                seen.add(dep)
                if dep in targets and dep != root:
                    found.setdefault(dep, root)
                queue.append((dep, tuple(req.extras)))
    return found
