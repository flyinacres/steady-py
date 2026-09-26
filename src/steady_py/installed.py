"""What is installed in the running interpreter: the frozen pin for each distribution, including
packages installed from a URL, a local path or an editable checkout (direct references)."""
import importlib.metadata
import json
import logging
import subprocess
import sys
import urllib.parse
from typing import Dict, FrozenSet, List, Optional, Tuple

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


def _direct_source_key(spec: str) -> Tuple[str, str]:
    """Host and path of a direct-reference spec, ignoring VCS prefix, @ref, #fragment and .git."""
    parts = urllib.parse.urlsplit(_strip_vcs_prefix(spec.strip().strip("'\"")))
    path = parts.path
    head, sep, tail = path.rpartition("@")
    if sep and "/" not in tail:  # a trailing @ref; user@host lives in netloc, not here
        path = head
    path = path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return parts.netloc.lower(), path.lower()


def same_direct_source(a: str, b: str) -> bool:
    """True if two direct-reference specs point at the same repository/archive, whatever ref each pins."""
    return _direct_source_key(a) == _direct_source_key(b)


def direct_reference_note(direct_url: str) -> str:
    """Plain-language description of where a non-PyPI package came from, for generated comments.
    Never includes the URL or path itself."""
    if is_local_direct_url(direct_url):
        return ("found on a system-dependent path, which can't and shouldn't be shared directly. "
                "Publish it or host it at a shared URL so others can install it")
    return ("installed from a direct URL, not PyPI; it is installed from the non-standard sources "
            "list, so make sure anyone running this notebook can reach that source")


def _read_direct_reference_pins() -> Dict[str, str]:
    """Finds installed packages that came from a direct reference (PEP 610 direct_url.json).

    Covers git, archive URL, local directory and editable installs uniformly, which
    parsing `pip freeze` text does not (an editable install prints a comment line and a
    bare `-e path`). Returns canonical name -> 'name @ url', with VCS installs written
    as 'vcs+url@commit' the way pip freeze does.
    """
    pins: Dict[str, str] = {}
    for dist in importlib.metadata.distributions():
        try:
            raw = dist.read_text("direct_url.json")
            name = dist.metadata["Name"]
        except (OSError, ValueError) as e:
            logger.debug(f"Could not read direct_url.json for a distribution: {e}")
            continue
        if not raw or not name:
            continue
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.debug(f"Malformed direct_url.json for {name}: {e}")
            continue
        url = info.get("url") if isinstance(info, dict) else None
        if not url:
            continue
        vcs_info = info.get("vcs_info")
        if isinstance(vcs_info, dict) and vcs_info.get("vcs"):
            url = f"{vcs_info['vcs']}+{url}"
            if vcs_info.get("commit_id"):
                url = f"{url}@{vcs_info['commit_id']}"
        pins.setdefault(util.canonicalize_pkg_name(name), f"{name} @ {url}")
    return pins


def get_installed_environment() -> Tuple[Dict[str, str], List[str]]:
    """Runs pip freeze to get precise version snapshots of the active runtime.

    Ordinary installs map to 'name==version'. Packages installed from a direct
    reference (git/URL/local path/editable) map to 'name @ url'; see split_frozen_pin.
    """
    res = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True)
    if res.returncode != 0:
        logger.warning(f"⚠️ 'pip freeze' execution failed (exit code {res.returncode}). Active environment versions could not be captured.")
        return {}, []

    frozen: Dict[str, str] = {}
    for line in res.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue  # comments and `-e path` lines; editable installs are read from metadata instead
        if " @ " in stripped:
            name, _, _ = split_frozen_pin(stripped)
            frozen[util.canonicalize_pkg_name(name)] = stripped
        elif "==" in stripped:
            pkg, ver = stripped.split("==", 1)
            canon = util.canonicalize_pkg_name(pkg)
            frozen[canon] = stripped
            frozen[pkg.lower()] = stripped

    frozen.update(_read_direct_reference_pins())
    return frozen, res.stdout.splitlines()
