"""Cell magics and shell commands: pip, conda and system-package installs, index URLs, scoped pip
flags, and the auxiliary tools a notebook installs without importing."""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from steady_py import scanning, util
from steady_py.constants import SHELL_CELL_MAGICS
from steady_py.models import Cell, DiagnosticEvent, HarvestResult, InstallLine, PipInstallOccurrence


PIP_SINGLE_FLAGS: Set[str] = {
    "-u", "--upgrade", "-q", "--quiet", "--user", "--no-cache-dir",
    "--force-reinstall", "--no-deps", "--pre", "--break-system-packages"
}

PIP_VALUE_FLAGS: Set[str] = {
    "--extra-index-url", "--index-url", "-i", "-f", "--find-links", 
    "-t", "--target", "-e", "--editable", "-r", "--requirement"
}



SHELL_SPLIT_PATTERN = re.compile(r'\s*(?:&&|;|\||\|\|)\s*')
PIP_INSTALL_PATTERN = re.compile(r'^\s*(?:%pip|!pip|pip3?)\s+install\s+(.+)$')
SYSTEM_PKG_PATTERN = re.compile(r'^\s*(?:!|%%bash|%%sh)?\s*(?:apt-get|brew|yum)\s+install\s+(.+)$')
CONDA_INSTALL_PATTERN = re.compile(r'^\s*(?:%conda|!conda|conda)\s+install\s+(.+)$')

VCS_OR_PATH_PREFIXES: Tuple[str, ...] = (
    ".", "/", "\\", "git+", "hg+", "svn+", "bzr+", "http://", "https://"
)



@dataclass
class _RawInstall:
    spec: str
    cell: Cell
    line_idx: int


@dataclass
class _Harvest:
    """One walk over every cell line: install lines, pip occurrences and raw (VCS/URL/path) installs."""
    install_lines: List[InstallLine] = field(default_factory=list)
    occurrences: List[PipInstallOccurrence] = field(default_factory=list)
    raw_installs: List[_RawInstall] = field(default_factory=list)


def _install_tool(seg: str) -> Optional[str]:
    if SYSTEM_PKG_PATTERN.match(seg):
        return "system"
    if CONDA_INSTALL_PATTERN.match(seg):
        return "conda"
    if PIP_INSTALL_PATTERN.match(seg):
        return "pip"
    return None


def _harvest(code_sources: scanning.CellsLike) -> _Harvest:
    found = _Harvest()
    for cell_idx, cell in enumerate(scanning.as_cells(code_sources)):
        cell_type, clean_body, first_line = scanning.classify_cell_source(cell.source)
        if cell_type == "WRITEFILE":
            continue
        for line_idx, line in enumerate(clean_body.splitlines(), start=first_line):
            clean_line = line.strip()
            if not clean_line or clean_line.startswith('#') or clean_line in SHELL_CELL_MAGICS:
                continue
            for segment in SHELL_SPLIT_PATTERN.split(clean_line):
                seg = segment.strip()
                tool = _install_tool(seg)
                if tool is None:
                    continue
                found.install_lines.append(InstallLine(text=seg, tool=tool, cell=cell, line_idx=line_idx))
                pip_match = PIP_INSTALL_PATTERN.match(seg)
                if tool == "pip" and pip_match:
                    _parse_pip_args(pip_match.group(1), cell_idx, cell, line_idx, found)
    return found


def _parse_pip_args(args_str: str, cell_idx: int, cell: Cell, line_idx: int, found: _Harvest) -> None:
    tokens = args_str.split()
    line_flags: List[str] = []
    token_specs: List[Tuple[str, str, str]] = []

    # Pass 1: Harvest all flags across the command segment first
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"--extra-index-url", "--index-url", "-i", "-f", "--find-links"}:
            if i + 1 < len(tokens):
                line_flags.extend([token, tokens[i+1].strip("'\"")])
                i += 2
                continue
        elif token in PIP_VALUE_FLAGS:
            i += 2
            continue
        i += 1

    # Pass 2: Extract package names and specs
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"--extra-index-url", "--index-url", "-i", "-f", "--find-links"} or token in PIP_VALUE_FLAGS:
            i += 2
            continue
        elif token.startswith('-') or token.lower() in PIP_SINGLE_FLAGS:
            i += 1
            continue
        elif any(token.lower().startswith(p) for p in VCS_OR_PATH_PREFIXES):
            found.raw_installs.append(_RawInstall(token.strip("'\""), cell, line_idx))
            i += 1
            continue

        match = re.search(r'[<>=!~;\[#]', token)
        if match:
            split_idx = match.start()
            pkg_name = token[:split_idx].strip("'\"")
            v_spec = token[split_idx:].strip("'\"")
        else:
            pkg_name = token.strip("'\"")
            v_spec = ""

        if pkg_name:
            token_specs.append((token, pkg_name, v_spec))
        i += 1

    for raw_tok, pkg, v_spec in token_specs:
        found.occurrences.append(
            PipInstallOccurrence(
                cell_idx=cell_idx,
                line_idx=line_idx,
                raw_token=raw_tok,
                name=pkg,
                version_spec=v_spec,
                flags=list(line_flags),
                cell=cell,
            )
        )


def harvest_pip_install_occurrences(code_sources: scanning.CellsLike) -> Tuple[List[PipInstallOccurrence], List[str]]:
    """
    Structured PipInstallOccurrence records for every pip install, skipping %%writefile cells.

    Also returns raw_installs: the exact original text of any token that's a
    VCS/URL/local-path install (git+, http(s)://, ./path, etc). These can't be
    decomposed into a name+version pin without actually running pip -- a bare
    git URL has no name until cloned -- so they're preserved verbatim.
    """
    found = _harvest(code_sources)
    return found.occurrences, [raw.spec for raw in found.raw_installs]


def resolve_pip_occurrences(
    occurrences: List[PipInstallOccurrence],
    is_execution_ordered: bool = True
) -> Tuple[Dict[str, PipInstallOccurrence], List[DiagnosticEvent]]:
    """
    Applies atomic last-wins resolution across occurrences.
    The later occurrence completely replaces earlier occurrences (name, version, flags indivisibly).
    Emits synchronized confidence-hedged warnings on pin or flag conflicts.
    """
    resolved: Dict[str, PipInstallOccurrence] = {}
    conflict_warnings: List[DiagnosticEvent] = []
    seen_history: Dict[str, List[PipInstallOccurrence]] = {}

    for occ in occurrences:
        norm_key = util.canonicalize_pkg_name(occ.name)
        seen_history.setdefault(norm_key, []).append(occ)

    time_qualifier = scanning.get_timeline_context_label(is_execution_ordered)

    for norm_key, history in seen_history.items():
        winning_occ = history[-1]
        resolved[norm_key] = winning_occ
        resolved[winning_occ.name] = winning_occ

        if len(history) > 1:
            versions = [h.version_spec for h in history if h.version_spec]
            if len(set(versions)) > 1:
                conflict_warnings.append(
                    DiagnosticEvent.at(
                        winning_occ.cell, winning_occ.line_idx, "conflicting_pin",
                        f"Conflicting Explicit Pins for '{winning_occ.name}': Resolving to '{winning_occ.name}{winning_occ.version_spec}' ({time_qualifier}).",
                    )
                )

            flags_history = [tuple(h.flags) for h in history]
            if len(set(flags_history)) > 1:
                flags_display = " ".join(winning_occ.flags) if winning_occ.flags else "default index (no flags)"
                conflict_warnings.append(
                    DiagnosticEvent.at(
                        winning_occ.cell, winning_occ.line_idx, "conflicting_flags",
                        f"Conflicting Scoped Flags for '{winning_occ.name}': Overwriting earlier flags with '{flags_display}' ({time_qualifier}).",
                    )
                )

    return resolved, conflict_warnings


def harvest_scoped_cell_flags(code_sources: scanning.CellsLike) -> Dict[str, List[str]]:
    """Convenience delegate returning harvested scoped flags map directly."""
    occurrences, _raw_installs = harvest_pip_install_occurrences(code_sources)
    resolved, _ = resolve_pip_occurrences(occurrences)
    return {pkg: occ.flags for pkg, occ in resolved.items()}


def harvest_index_urls_from_sources(code_sources: scanning.CellsLike) -> Set[str]:
    """Scans code sources for index URLs and returns a combined set of all harvested URLs."""
    h_res = harvest_cell_magics_and_commands(code_sources)
    return h_res.base_index_urls.union(h_res.extra_index_urls)


def harvest_cell_magics_and_commands(
    code_sources: scanning.CellsLike
) -> HarvestResult:
    """Scans code sources for cell magics, index URLs, auxiliary tools, and shell commands."""
    found = _harvest(code_sources)
    occurrences = found.occurrences
    resolved_occs, magic_warnings = resolve_pip_occurrences(occurrences)

    harvested_packages: Set[str] = set()
    base_index_urls: Set[str] = set()
    extra_index_urls: Set[str] = set()
    magic_notices: List[DiagnosticEvent] = []
    scoped_flags: Dict[str, List[str]] = {}

    for raw in found.raw_installs:
        magic_notices.append(DiagnosticEvent.at(
            raw.cell, raw.line_idx, "raw_install",
            f"'{raw.spec}' is installed from a non-standard source (git/URL/local file), not PyPI. "
            f"It will still be installed exactly as specified, but can't be verified or checked for "
            f"drift -- you're responsible for ensuring anyone running this notebook has access to "
            f"the same resource.",
            level="notice",
        ))

    for occ in occurrences:
        harvested_packages.add(occ.name)

    for occ in resolved_occs.values():
        scoped_flags[occ.name] = occ.flags
        i = 0
        while i < len(occ.flags):
            flag = occ.flags[i]
            val = occ.flags[i+1] if i + 1 < len(occ.flags) else ""
            if flag in {"--index-url", "-i"}:
                base_index_urls.add(val)
            elif flag in {"--extra-index-url", "-f", "--find-links"}:
                extra_index_urls.add(val)
            i += 2

    for line in found.install_lines:
        if line.tool == "system":
            magic_notices.append(DiagnosticEvent.at(
                line.cell, line.line_idx, "system_command",
                f"Uses a system install command ('{line.text}'). Note: System dependencies must be run manually by readers.",
                level="notice",
            ))
        elif line.tool == "conda":
            magic_notices.append(DiagnosticEvent.at(
                line.cell, line.line_idx, "conda_command",
                "Uses 'conda install'. Conda packages are not tracked in pip requirements manifests.",
                level="notice",
            ))
        elif "-r " in line.text or "--requirement" in line.text:
            magic_warnings.append(DiagnosticEvent.at(
                line.cell, line.line_idx, "external_requirement",
                f"References an external requirements file ('{line.text}'). Ensure that file is shared alongside your notebook.",
            ))

    return HarvestResult(
        harvested_packages=harvested_packages,
        base_index_urls=base_index_urls,
        extra_index_urls=extra_index_urls,
        magic_warnings=magic_warnings,
        magic_notices=magic_notices,
        scoped_flags=scoped_flags,
        raw_installs=[raw.spec for raw in found.raw_installs],
        install_lines=found.install_lines,
    )
