"""Drift checks against PyPI: per-pin signals (yanked or removed, stale, major bump, Python support),
the transitive graph resolved with resolvelib, the generation-time baseline, and the per-notebook
and batch drift reports those feed."""
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from resolvelib import AbstractProvider, BaseReporter, Resolver
from resolvelib.resolvers import ResolutionImpossible

from steady_py import installed, models, pypi, util
from steady_py.constants import BaselineStatus, FetchStatus, ReportKind, Severity, Signal
from steady_py.models import Baseline, DriftFinding, FindingKey, PinnedDependency, SteadyPyManifest

logger = logging.getLogger("steady_py.drift")


# --- Direct-pin checks ---------------------------------------------------
# Heuristic thresholds below are deliberately simple defaults, not tuned
# against corpus data yet -- easy to revisit once Check mode runs against
# real notebooks.
STALE_THRESHOLD_DAYS = 730  # ~2 years with no release anywhere in the project


def _pip_env_hint() -> str:
    """Checks the CURRENT process's environment for pip index-related variables
    that could explain a package resolving locally despite PyPI having no record
    of it. This only describes THIS environment -- it says nothing about whether
    anyone else running the notebook would have the same variables set, which is
    exactly why the underlying finding stays confirmed regardless of this hint.
    """
    relevant = ["PIP_FIND_LINKS", "PIP_NO_INDEX", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL"]
    set_vars = [(k, os.environ[k]) for k in relevant if os.environ.get(k)]
    if not set_vars:
        return ""
    parts = ", ".join(f"{k}={v}" for k, v in set_vars)
    return f" Note: {parts} is set in this environment, which may explain this."


def _marker_environment(required_python: Dict[str, int], extra: Optional[str]) -> Dict[str, str]:
    """Real evaluation environment for a requires_dist marker: actual REQUIRED_PYTHON,
    the pin's own extra (or none -- a base install activates no extras), and
    packaging's default_environment() for everything else (platform/OS markers;
    Kaggle/Colab are Linux, matching this environment, a reasonable approximation).
    """
    py_version = f"{required_python.get('major')}.{required_python.get('minor')}"
    env: Dict[str, str] = {str(k): str(v) for k, v in default_environment().items()}
    env["python_version"] = py_version
    env["python_full_version"] = py_version
    env["extra"] = extra or ""
    return env


def _local_find_links_artifact_hint(name: str, version: str) -> str:
    """Checks PIP_FIND_LINKS for a local directory that actually contains a
    matching wheel/sdist for this pin, so the not-found-on-PyPI message can
    state a verified fact instead of only noting that an env var is set.

    Local paths only -- no network. PIP_FIND_LINKS may also list URLs; those
    are left untouched (verifying them would require a network probe, which
    is a separate, deferred concern with its own risk profile).
    """
    find_links = os.environ.get("PIP_FIND_LINKS", "")
    if not find_links:
        return ""
    normalized = name.replace("-", "_").replace(".", "_")
    for target in find_links.split():
        if target.startswith(("http://", "https://")):
            continue
        target_dir = Path(target)
        if not target_dir.is_dir():
            continue
        matches = (
            list(target_dir.glob(f"{normalized}-{version}*.whl"))
            + list(target_dir.glob(f"{name}-{version}*.tar.gz"))
            + list(target_dir.glob(f"{name}-{version}*.zip"))
        )
        if matches:
            return f" Verified present in {target}: {matches[0].name}."
        return f" Checked {target} -- no matching artifact for {name}=={version} found there."
    return ""


def check_yanked_or_removed(name: str, version: str) -> List[DriftFinding]:
    """Distinguishes: pin still resolvable -> yanked or clean; pin gone but project alive -> removed;
    whole project gone -> removed (project-level); any network failure -> check_error, not silence.
    """
    name, _ = installed.split_pin_name(name)
    version_meta = pypi.fetch_pypi_version_metadata(name, version)

    if version_meta.status == FetchStatus.NETWORK_ERROR:
        return [DriftFinding(
            package=name, version=version, signal=Signal.CHECK_ERROR, severity=Severity.ERROR,
            message=f"Could not check {name}=={version} against PyPI: {version_meta.error_detail}",
        )]

    if version_meta.status == FetchStatus.FOUND:
        if version_meta.yanked:
            reason = f" ({version_meta.yanked_reason})" if version_meta.yanked_reason else ""
            return [DriftFinding(
                package=name, version=version, signal=Signal.YANKED, severity=Severity.CONFIRMED,
                message=f"{name}=={version} has been yanked from PyPI{reason}",
                details={"yanked_reason": version_meta.yanked_reason},
            )]
        return []

    # version_meta.status == FetchStatus.NOT_FOUND: disambiguate version-removed vs. project-removed
    package_meta = pypi.fetch_pypi_package_metadata(name)
    if package_meta.status == FetchStatus.NETWORK_ERROR:
        return [DriftFinding(
            package=name, version=version, signal=Signal.CHECK_ERROR, severity=Severity.ERROR,
            message=f"Could not check {name}=={version} against PyPI: {package_meta.error_detail}",
        )]
    if package_meta.status == FetchStatus.FOUND:
        return [DriftFinding(
            package=name, version=version, signal=Signal.REMOVED, severity=Severity.CONFIRMED,
            message=f"{name}=={version} no longer exists on PyPI, though {name} itself is still published",
        )]

    env_hint = _pip_env_hint()
    local_hint = _local_find_links_artifact_hint(name, version)
    return [DriftFinding(
        package=name, version=version, signal=Signal.NOT_FOUND_ON_PYPI, severity=Severity.CONFIRMED,
        message=(
            f"{name} could not be found on PyPI. It may be a private, local-only, or custom-index "
            f"package that ships alongside this notebook (if so, no action needed), or the name may "
            f"be misspelled.{env_hint}{local_hint}"
        ),
        details={"pip_env_hint": env_hint, "local_artifact_hint": local_hint} if (env_hint or local_hint) else {},
    )]


def check_staleness(name: str, version: str) -> List[DriftFinding]:
    """Heuristic: no release anywhere in the project within STALE_THRESHOLD_DAYS."""
    name, _ = installed.split_pin_name(name)
    package_meta = pypi.fetch_pypi_package_metadata(name)
    if package_meta.status == FetchStatus.NETWORK_ERROR:
        return [DriftFinding(
            package=name, version=version, signal=Signal.CHECK_ERROR, severity=Severity.ERROR,
            message=f"Could not check {name} for staleness: {package_meta.error_detail}",
        )]
    if package_meta.status != FetchStatus.FOUND or not package_meta.releases:
        return []

    upload_times = []
    for info in package_meta.releases.values():
        ts = info.get("upload_time")
        if not ts:
            continue
        try:
            upload_times.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
        except ValueError:
            continue
    if not upload_times:
        return []

    most_recent = max(upload_times)
    age_days = (datetime.now(most_recent.tzinfo) - most_recent).days
    if age_days < STALE_THRESHOLD_DAYS:
        return []

    return [DriftFinding(
        package=name, version=version, signal=Signal.STALE, severity=Severity.HEURISTIC,
        message=f"{name} has had no release in {age_days} days (last: {most_recent.date().isoformat()}) -- worth reviewing whether it's still maintained",
        details={"days_since_last_release": age_days, "last_release_date": most_recent.date().isoformat()},
    )]


def check_major_bump(name: str, version: str) -> List[DriftFinding]:
    """Heuristic: a newer major version exists than the one pinned -- worth reviewing, not a failure."""
    name, _ = installed.split_pin_name(name)
    package_meta = pypi.fetch_pypi_package_metadata(name)
    if package_meta.status == FetchStatus.NETWORK_ERROR:
        return [DriftFinding(
            package=name, version=version, signal=Signal.CHECK_ERROR, severity=Severity.ERROR,
            message=f"Could not check {name} for a newer major version: {package_meta.error_detail}",
        )]
    if package_meta.status != FetchStatus.FOUND or not package_meta.latest_version:
        return []
    try:
        pinned_v, latest_v = Version(version), Version(package_meta.latest_version)
    except InvalidVersion:
        return []
    if latest_v.major <= pinned_v.major:
        return []

    return [DriftFinding(
        package=name, version=version, signal=Signal.MAJOR_BUMP, severity=Severity.HEURISTIC,
        message=f"{name}=={version} is on major version {pinned_v.major}; {package_meta.latest_version} (major {latest_v.major}) is available -- worth reviewing",
        latest_version=package_meta.latest_version,
    )]


def check_python_support(name: str, version: str, required_python: Dict[str, int]) -> List[DriftFinding]:
    """Confirms the pinned release declares support for the notebook's REQUIRED_PYTHON."""
    name, _ = installed.split_pin_name(name)
    version_meta = pypi.fetch_pypi_version_metadata(name, version)
    if version_meta.status == FetchStatus.NETWORK_ERROR:
        return [DriftFinding(
            package=name, version=version, signal=Signal.CHECK_ERROR, severity=Severity.ERROR,
            message=f"Could not check {name}=={version} for Python support: {version_meta.error_detail}",
        )]
    if version_meta.status != FetchStatus.FOUND or not version_meta.requires_python:
        return []  # nothing declared -> nothing to confirm against; not a finding

    target = f"{required_python.get('major')}.{required_python.get('minor')}"
    try:
        supported = SpecifierSet(version_meta.requires_python).contains(target, prereleases=True)
    except InvalidSpecifier:
        return []

    if supported:
        return []
    return [DriftFinding(
        package=name, version=version, signal=Signal.UNSUPPORTED_PYTHON, severity=Severity.CONFIRMED,
        message=f"{name}=={version} declares requires-python {version_meta.requires_python}, which does not cover Python {target}",
        details={"declared_requires_python": version_meta.requires_python, "notebook_python": target},
    )]


# --- Transitive resolution (resolvelib) -----------------------------------
# Resolves the FULL dependency graph (direct pins + everything transitively
# required) purely from PyPI-published metadata: what versions exist, and
# what each declares via requires_dist. No local install, no platform/wheel
# matching -- that's what makes this usable for Kaggle/Colab targets rather
# than whatever platform steady-py itself happens to run on. Uses resolvelib,
# the same PyPA resolution algorithm pip has used internally since pip 20.3,
# rather than a hand-rolled approximation of what a real resolver does.
#
# Extras (e.g. "pandas[test]", on a direct pin or named by any package's own
# requires_dist) are modeled the way pip's resolver does: "pandas[test]" is its
# own node in the graph. It depends on the base package at the same version plus
# every requirement gated on that extra, so the extra's requirements are walked
# and version conflicts through them are detected. The extras node is an
# internal device and never appears in the resolved mapping.

@dataclass(frozen=True)
class _ResolutionCandidate:
    """A concrete (name, version[, extras]) resolvelib candidate backed by live PyPI data."""
    name: str
    version: str
    extras: FrozenSet[str] = frozenset()


def _resolution_identifier(name: str, extras) -> str:
    """Graph node id: the canonical name, plus a sorted extras suffix when extras are requested."""
    base = util.canonicalize_pkg_name(name)
    if not extras:
        return base
    return f"{base}[{','.join(sorted(util.canonicalize_pkg_name(e) for e in extras))}]"


class _PyPIResolutionProvider(AbstractProvider):
    """resolvelib Provider backed entirely by fetch_pypi_* -- no local environment."""

    def __init__(self, required_python: Dict[str, int]):
        self.required_python = required_python

    def identify(self, requirement_or_candidate) -> str:
        return _resolution_identifier(requirement_or_candidate.name, requirement_or_candidate.extras)

    def get_preference(self, identifier, resolutions, candidates, information, backtrack_causes) -> int:
        return len(list(candidates[identifier]))

    def find_matches(self, identifier, requirements, incompatibilities) -> List[_ResolutionCandidate]:
        reqs = list(requirements[identifier])
        if not reqs:
            return []
        name = reqs[0].name
        extras = frozenset(reqs[0].extras)  # identical across reqs: extras are part of the identifier
        pkg_meta = pypi.fetch_pypi_package_metadata(name)
        if pkg_meta.status != FetchStatus.FOUND:
            return []
        excluded = {c.version for c in incompatibilities[identifier]}
        matches = []
        for ver_str in pkg_meta.releases:
            if ver_str in excluded:
                continue
            try:
                v = Version(ver_str)
            except InvalidVersion:
                continue
            if all(r.specifier.contains(v, prereleases=True) for r in reqs):
                matches.append((v, ver_str))
        matches.sort(key=lambda pair: pair[0], reverse=True)
        return [_ResolutionCandidate(name, ver_str, extras) for _, ver_str in matches]

    def is_satisfied_by(self, requirement, candidate) -> bool:
        try:
            return requirement.specifier.contains(Version(candidate.version), prereleases=True)
        except InvalidVersion:
            return False

    def get_dependencies(self, candidate) -> List[Requirement]:
        deps: List[Requirement] = []
        if candidate.extras:
            # The extras node rides on the base package at exactly the same version.
            deps.append(Requirement(f"{candidate.name}=={candidate.version}"))
            envs = [_marker_environment(self.required_python, extra=e) for e in sorted(candidate.extras)]
        else:
            envs = [_marker_environment(self.required_python, extra=None)]  # base install: no extras active

        meta = pypi.fetch_pypi_version_metadata(candidate.name, candidate.version)
        if meta.status != FetchStatus.FOUND:
            return deps
        for raw in meta.requires_dist:
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                continue
            if req.marker is not None and not any(req.marker.evaluate(env) for env in envs):
                continue
            deps.append(req)
        return deps


def resolve_transitive_graph(
    dependencies: List[PinnedDependency], required_python: Dict[str, int]
) -> Tuple[Optional[Dict[str, str]], List[DriftFinding]]:
    """Resolves direct pins + everything transitively required, from PyPI metadata alone.

    Returns (resolved_versions, findings). On success: resolved_versions maps every
    package name in the graph to the version resolvelib picked, findings is empty.
    On an unsatisfiable graph: resolved_versions is None, findings has one confirmed
    "conflict" finding per underlying cause resolvelib reports.
    """
    root_reqs = []
    for dep in dependencies:
        raw_name, version = dep.name, dep.version
        if not raw_name or not version:
            continue
        if installed.has_local_version_identifier(version):
            continue  # not on PyPI by definition -- can't be a root requirement here
        name, extras = installed.split_pin_extras(raw_name)
        if pypi.fetch_pypi_package_metadata(name).status != FetchStatus.FOUND:
            # Custom-index/local-only package: not resolvable via this PyPI-only
            # provider, and not a real conflict -- check_yanked_or_removed already
            # reports on it directly (not_found_on_pypi), so silently excluding it
            # from the graph here avoids a false ResolutionImpossible.
            continue
        extras_part = f"[{','.join(sorted(extras))}]" if extras else ""
        try:
            root_reqs.append(Requirement(f"{name}{extras_part}=={version}"))
        except InvalidRequirement:
            continue

    provider = _PyPIResolutionProvider(required_python)
    resolver = Resolver(provider, BaseReporter())

    try:
        result = resolver.resolve(root_reqs)
    except ResolutionImpossible as e:
        findings = [
            DriftFinding(
                package=getattr(cause.requirement, "name", "?"),
                version=str(getattr(cause.requirement, "specifier", "")),
                signal=Signal.CONFLICT, severity=Severity.CONFIRMED,
                message=(
                    f"Unresolvable dependency graph: {cause.requirement} required by "
                    f"{cause.parent.name if cause.parent else 'a direct pin'}"
                ),
                parent=cause.parent.name if cause.parent else "",
            )
            for cause in e.causes
        ]
        return None, findings
    except Exception as e:
        return None, [DriftFinding(
            package="", version="", signal=Signal.CHECK_ERROR, severity=Severity.ERROR,
            message=f"Transitive resolution failed unexpectedly: {type(e).__name__}: {e}",
        )]

    # Extras nodes are internal: each one's base package (same version) is also in the mapping.
    return {name: cand.version for name, cand in result.mapping.items() if "[" not in name}, []


def check_transitive_signals(
    dependencies: List[PinnedDependency], required_python: Dict[str, int]
) -> List[DriftFinding]:
    """Resolves the full graph, then runs yanked/removed, staleness, major-bump, and
    python-support against every transitively-discovered package's resolved version.
    Direct pins are skipped -- already covered by the direct-pin checks against
    their real pinned version, not a resolver-picked one.
    """
    resolved, findings = resolve_transitive_graph(dependencies, required_python)
    if resolved is None:
        return findings  # unresolvable -- conflict findings already built

    direct_names = {
        util.canonicalize_pkg_name(installed.split_pin_name(d.name)[0])
        for d in dependencies if d.name
    }

    for name, version in resolved.items():
        if util.canonicalize_pkg_name(name) in direct_names:
            continue
        findings.extend(check_yanked_or_removed(name, version))
        findings.extend(check_staleness(name, version))
        findings.extend(check_major_bump(name, version))
        findings.extend(check_python_support(name, version, required_python))

    return findings


def run_pin_checks(dependencies: List[PinnedDependency], python_version: Dict[str, int]) -> List[DriftFinding]:
    """Every PyPI-based check against a list of pins, direct and transitive.

    The single implementation behind both generation-time validation and --check-drift.
    Keeping them one function is deliberate: they used to be two hand-maintained copies of
    the same sequence, and any recorded-versus-current comparison is only meaningful if both
    sides produce findings the same way.
    """
    findings: List[DriftFinding] = []
    for dep in dependencies:
        name, version = dep.name, dep.version
        if not name or not version:
            continue
        if installed.has_local_version_identifier(version):
            findings.append(DriftFinding(
                package=name, version=version, signal=Signal.UNVERIFIABLE_CUSTOM_INDEX, severity=Severity.HEURISTIC,
                message=f"{name}=={version} has a local version identifier -- installed from a custom index, "
                        f"not PyPI, so PyPI-based checks (yanked/removed/staleness/major-bump/python-support) "
                        f"cannot be run against it.",
            ))
            continue
        findings.extend(check_yanked_or_removed(name, version))
        findings.extend(check_staleness(name, version))
        findings.extend(check_major_bump(name, version))
        findings.extend(check_python_support(name, version, python_version))
    findings.extend(check_transitive_signals(dependencies, python_version))
    return findings


# --- Generation-time baseline ------------------------------------------------
# Generation records which findings already existed, as compact keys (never messages,
# which embed changing dates and counts). A later check classifies each finding as
# "known" (its key was recorded), "new", or "not_checked_at_generation" (its package
# could not be checked back then, so "new" would be a claim we can't make). Each signal
# keys on exactly the facts that define the problem: a changed fact is a new finding.


def build_baseline(findings: List[DriftFinding]) -> Baseline:
    """Compact, sorted record of generation-time findings, plus the packages that could not
    be checked ("" means the transitive resolution itself failed)."""
    keys: Set[FindingKey] = set()
    errored: Set[str] = set()
    for f in findings:
        if f.severity == Severity.ERROR:
            errored.add(f.package)
            continue
        key = models.finding_baseline_key(f)
        if key is not None:
            keys.add(key)
    return Baseline(findings=tuple(sorted(keys)), errors=tuple(sorted(errored)))


def classify_against_baseline(findings: List[DriftFinding], manifest: SteadyPyManifest) -> bool:
    """Sets baseline_status on every comparable finding. Returns False (and touches nothing)
    when the manifest has no usable baseline."""
    if manifest.baseline is None:
        return False
    known = set(manifest.baseline.findings)
    errored = {util.canonicalize_pkg_name(e) if e else "" for e in manifest.baseline.errors}
    direct = {
        util.canonicalize_pkg_name(installed.split_pin_name(d.name)[0]) for d in manifest.dependencies if d.name
    }
    graph_check_failed = "" in errored
    for f in findings:
        key = models.finding_baseline_key(f)
        if key is None:
            continue
        canon = util.canonicalize_pkg_name(f.package)
        if key in known:
            f.baseline_status = BaselineStatus.KNOWN
            if f.signal == Signal.NOT_FOUND_ON_PYPI:
                # Already true at generation, so this is a custom source (private or custom-index
                # package), an expected state rather than drift. Shown as a notice, never fails
                # the check. The same finding appearing NEW means a package that was on PyPI has
                # vanished, which stays a confirmed failure.
                f.severity = Severity.NOTICE
        elif canon in errored or (graph_check_failed and canon not in direct):
            f.baseline_status = BaselineStatus.NOT_CHECKED_AT_GENERATION
        else:
            f.baseline_status = BaselineStatus.NEW
    return True


# --- Report shape ----------------------------------------------------------
# Confirmed findings (yanked, removed, declared conflict, unsupported-python)
# are visually separated from heuristic findings (stale, major-bump) per this
# project's stated principle: mixing severities erodes trust. Errors (a pin
# that couldn't be checked at all) get their own section too -- never folded
# into "no drift found."

@dataclass
class DriftCheckReport:
    """Aggregated result for one manifest. kind distinguishes two genuinely different
    moments: "validation" (generation time -- brand-new pins, checked for the first
    time, nothing has elapsed) vs "check" (--check-drift -- a previously-frozen
    manifest, re-checked against however PyPI has moved since). Same underlying
    checks either way; "drift" as a word only means something for the second one.
    """
    target: str
    checked_at: str
    manifest: SteadyPyManifest
    kind: str = ReportKind.CHECK  # ReportKind.VALIDATION | ReportKind.CHECK
    confirmed: List[DriftFinding] = field(default_factory=list)
    heuristic: List[DriftFinding] = field(default_factory=list)
    errors: List[DriftFinding] = field(default_factory=list)
    notices: List[DriftFinding] = field(default_factory=list)  # known custom sources; never affect the outcome
    baseline_recorded: bool = False  # True only when findings were classified against a usable baseline

    @property
    def has_confirmed(self) -> bool:
        return len(self.confirmed) > 0

    @property
    def has_actionable_heuristic(self) -> bool:
        """A heuristic finding that should fail a check: anything not already known at generation.
        Without a baseline nothing is known, so every heuristic finding counts."""
        return any(f.baseline_status != BaselineStatus.KNOWN for f in self.heuristic)

    def baseline_counts(self) -> Dict[str, int]:
        counts = {BaselineStatus.NEW: 0, BaselineStatus.KNOWN: 0, BaselineStatus.NOT_CHECKED_AT_GENERATION: 0}
        for f in self.confirmed + self.heuristic + self.notices:
            if f.baseline_status in counts:
                counts[f.baseline_status] += 1
        return counts

    @property
    def has_heuristic(self) -> bool:
        return len(self.heuristic) > 0

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    @property
    def is_clean(self) -> bool:
        return not (self.has_confirmed or self.has_heuristic or self.has_errors)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "kind": self.kind,
            "target": self.target,
            "checked_at": self.checked_at,
            "manifest": self.manifest.to_dict(),
            "confirmed": [f.to_dict() for f in self.confirmed],
            "heuristic": [f.to_dict() for f in self.heuristic],
            "errors": [f.to_dict() for f in self.errors],
        }
        if self.kind == ReportKind.CHECK:
            out["notices"] = [f.to_dict() for f in self.notices]
            out["baseline"] = {"recorded": True, **self.baseline_counts()} if self.baseline_recorded else {"recorded": False}
        return out


def build_drift_check_report(
    target: str, manifest: SteadyPyManifest, findings: List[DriftFinding], kind: str = "check"
) -> DriftCheckReport:
    """Buckets a flat findings list into confirmed/heuristic/error by severity. For a check
    (not generation-time validation), findings are first classified against the manifest's
    baseline, and each bucket lists new findings before known ones."""
    baseline_recorded = classify_against_baseline(findings, manifest) if kind == ReportKind.CHECK else False
    report = DriftCheckReport(
        target=target,
        checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        manifest=manifest,
        kind=kind,
        baseline_recorded=baseline_recorded,
    )
    for f in findings:
        if f.severity == Severity.CONFIRMED:
            report.confirmed.append(f)
        elif f.severity == Severity.HEURISTIC:
            report.heuristic.append(f)
        elif f.severity == Severity.NOTICE:
            report.notices.append(f)
        else:
            report.errors.append(f)
    report.confirmed.sort(key=lambda f: f.baseline_status == BaselineStatus.KNOWN)  # stable: new first, known last
    report.heuristic.sort(key=lambda f: f.baseline_status == BaselineStatus.KNOWN)
    return report


# --- Batch aggregate validation ---------------------------------------------
# Generation-time validation happens once per notebook when a batch writes its locked files.
# This folds those per-notebook reports into one repository-level view: each distinct finding
# once, with the notebooks it affects. Everything in it is deterministic (sorted, relative
# paths, no timestamps), so two runs can be compared directly.

_VALIDATION_SEVERITY_ORDER = [Severity.CONFIRMED, Severity.HEURISTIC, Severity.ERROR]


@dataclass
class BatchFindingGroup:
    """One distinct finding, and every notebook in the batch it appears in."""
    key: FindingKey
    signal: str
    severity: str
    package: str
    version: str
    message: str
    notebooks: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": list(self.key), "signal": self.signal, "severity": self.severity,
            "package": self.package, "version": self.version, "message": self.message,
            "notebooks": self.notebooks,
        }


@dataclass
class NotebookValidationCounts:
    """One checked notebook's finding counts, by severity."""
    path: str  # relative to the batch directory
    confirmed: int = 0
    heuristic: int = 0
    errors: int = 0

    @property
    def has_findings(self) -> bool:
        return bool(self.confirmed or self.heuristic or self.errors)

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "confirmed": self.confirmed, "heuristic": self.heuristic, "errors": self.errors}


@dataclass
class BatchValidation:
    findings: List[BatchFindingGroup]
    notebooks: List[NotebookValidationCounts]  # every notebook checked

    @property
    def notebooks_checked(self) -> int:
        return len(self.notebooks)

    def totals(self) -> Dict[str, int]:
        return {
            "confirmed": sum(1 for g in self.findings if g.severity == Severity.CONFIRMED),
            "heuristic": sum(1 for g in self.findings if g.severity == Severity.HEURISTIC),
            "errors": sum(1 for g in self.findings if g.severity == Severity.ERROR),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "notebooks_checked": self.notebooks_checked,
            "totals": self.totals(),
            "findings": [g.to_dict() for g in self.findings],
            "notebooks": [n.to_dict() for n in self.notebooks],
        }


def build_batch_validation(reports: List[Tuple[str, "DriftCheckReport"]]) -> BatchValidation:
    """Groups per-notebook validation reports, given as (relative path, report), by distinct finding.
    Findings group on their identity key plus message: within one run the same package, version
    and signal always produce the same message, and distinct errors on one package stay separate."""
    groups: Dict[Tuple[str, Tuple[str, ...], str], BatchFindingGroup] = {}
    notebooks: List[NotebookValidationCounts] = []
    for path, report in sorted(reports, key=lambda item: item[0]):
        notebooks.append(NotebookValidationCounts(
            path=path,
            confirmed=len(report.confirmed),
            heuristic=len(report.heuristic),
            errors=len(report.errors),
        ))
        for f in report.confirmed + report.heuristic + report.errors:
            key = models.finding_identity_key(f)
            gkey = (f.severity, tuple(key), f.message)
            group = groups.get(gkey)
            if group is None:
                group = groups[gkey] = BatchFindingGroup(
                    key=key, signal=f.signal, severity=f.severity,
                    package=f.package, version=f.version, message=f.message,
                )
            if path not in group.notebooks:
                group.notebooks.append(path)

    def order(g: BatchFindingGroup):
        sev = _VALIDATION_SEVERITY_ORDER.index(g.severity) if g.severity in _VALIDATION_SEVERITY_ORDER else len(_VALIDATION_SEVERITY_ORDER)
        return (sev, g.key, g.message)

    return BatchValidation(findings=sorted(groups.values(), key=order), notebooks=notebooks)
