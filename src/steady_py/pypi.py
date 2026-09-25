"""Read-only PyPI JSON metadata client used by drift checks."""
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from steady_py import util
from steady_py.constants import TOOL_VERSION, FetchStatus


# =====================================================================
# DRIFT-CHECK: PYPI METADATA CLIENT
# =====================================================================
# Read-only lookups against live PyPI JSON metadata, used by drift-check
# (Check mode). Never installs, never executes anything from a response.
# Two independent caches, scoped to a single run only (cleared per process,
# never persisted): version-specific data and package-level data answer
# different questions and are fetched from different PyPI endpoints.

PYPI_REQUEST_TIMEOUT = 10
PYPI_USER_AGENT = f"steady-py-drift-check/{TOOL_VERSION}"

# Lookup status: "found", "not_found" (404), or "network_error" (offline,
# timeout, malformed response). "not_found" and "network_error" are kept
# distinct from each other and from a clean "found" -- a network failure
# must never be reported or treated as "no drift found."


@dataclass
class PypiVersionMetadata:
    """Result of looking up one exact (package, version) pin."""
    status: str
    requires_dist: List[str] = field(default_factory=list)
    requires_python: Optional[str] = None
    yanked: bool = False
    yanked_reason: Optional[str] = None
    project_urls: Dict[str, str] = field(default_factory=dict)
    error_detail: Optional[str] = None


@dataclass
class PypiPackageMetadata:
    """Result of looking up a package's project-level (version-independent) data."""
    status: str
    latest_version: Optional[str] = None
    releases: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # version -> {"upload_time": str, "yanked": bool}
    error_detail: Optional[str] = None


def _fetch_pypi_json(url: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """Shared HTTP GET against a PyPI JSON endpoint. Returns (status, payload, error_detail)."""
    req = urllib.request.Request(url, headers={"User-Agent": PYPI_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=PYPI_REQUEST_TIMEOUT) as resp:
            return FetchStatus.FOUND, json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return FetchStatus.NOT_FOUND, None, None
        return FetchStatus.NETWORK_ERROR, None, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return FetchStatus.NETWORK_ERROR, None, str(e)


@util._memoize_for_run
def fetch_pypi_version_metadata(name: str, version: str) -> PypiVersionMetadata:
    """Looks up one exact pinned release. Cache key: (name, version) -- invariant across notebooks."""
    status, payload, error_detail = _fetch_pypi_json(f"https://pypi.org/pypi/{name}/{version}/json")
    if status != FetchStatus.FOUND:
        return PypiVersionMetadata(status=status, error_detail=error_detail)
    assert payload is not None  # _fetch_pypi_json returns a payload whenever the status is FOUND

    info = payload.get("info", {})
    return PypiVersionMetadata(
        status=FetchStatus.FOUND,
        requires_dist=info.get("requires_dist") or [],
        requires_python=info.get("requires_python"),
        yanked=info.get("yanked", False),
        yanked_reason=info.get("yanked_reason"),
        project_urls=info.get("project_urls") or {},
    )


@util._memoize_for_run
def fetch_pypi_package_metadata(name: str) -> PypiPackageMetadata:
    """Looks up a package's project-level data (latest version, full release history). Cache key: name alone."""
    status, payload, error_detail = _fetch_pypi_json(f"https://pypi.org/pypi/{name}/json")
    if status != FetchStatus.FOUND:
        return PypiPackageMetadata(status=status, error_detail=error_detail)
    assert payload is not None  # _fetch_pypi_json returns a payload whenever the status is FOUND

    info = payload.get("info", {})
    releases: Dict[str, Dict[str, Any]] = {}
    for ver, files in (payload.get("releases") or {}).items():
        if not files:
            continue
        releases[ver] = {
            "upload_time": files[0].get("upload_time_iso_8601"),
            "yanked": any(f.get("yanked") for f in files),
        }

    return PypiPackageMetadata(
        status=FetchStatus.FOUND,
        latest_version=info.get("version"),
        releases=releases,
    )
