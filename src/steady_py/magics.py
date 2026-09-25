"""Cell magics and shell commands: pip, conda and system-package installs, index URLs, scoped pip
flags, and the auxiliary tools a notebook installs without importing."""
import re
from typing import Dict, List, Set, Tuple

from steady_py import scanning, util
from steady_py.constants import SHELL_CELL_MAGICS
from steady_py.models import DiagnosticEvent, HarvestResult, PipInstallOccurrence


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



def harvest_pip_install_occurrences(code_sources: List[str]) -> Tuple[List[PipInstallOccurrence], List[str]]:
    """
    Walks all cell lines and extracts structured PipInstallOccurrence records.
    Filters out %%writefile cells completely.

    Also returns raw_installs: the exact original text of any token that's a
    VCS/URL/local-path install (git+, http(s)://, ./path, etc). These can't be
    decomposed into a name+version pin without actually running pip -- a bare
    git URL has no name until cloned -- so they're preserved verbatim instead
    of being forced into the wrong shape. Previously these were silently
    dropped entirely, meaning the generated Cell 2 would never attempt to
    install them at all.
    """
    occurrences: List[PipInstallOccurrence] = []
    raw_installs: List[str] = []

    for cell_idx, source in enumerate(code_sources):
        cell_type, clean_body = scanning.classify_cell_source(source)
        if cell_type == "WRITEFILE":
            continue

        for line_idx, line in enumerate(clean_body.splitlines()):
            clean_line = line.strip()
            if not clean_line or clean_line.startswith('#') or clean_line in SHELL_CELL_MAGICS:
                continue

            command_segments = SHELL_SPLIT_PATTERN.split(clean_line)
            for segment in command_segments:
                seg = segment.strip()
                pip_match = PIP_INSTALL_PATTERN.match(seg)
                if not pip_match:
                    continue

                args_str = pip_match.group(1)
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
                        raw_installs.append(token.strip("'\""))
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
                    occurrences.append(
                        PipInstallOccurrence(
                            cell_idx=cell_idx,
                            line_idx=line_idx,
                            raw_token=raw_tok,
                            name=pkg,
                            version_spec=v_spec,
                            flags=list(line_flags)
                        )
                    )

    return occurrences, raw_installs


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
                    DiagnosticEvent(
                        type="conflicting_pin",
                        detail=f"Conflicting Explicit Pins for '{winning_occ.name}': Resolving to '{winning_occ.name}{winning_occ.version_spec}' ({time_qualifier}).",
                        cell_idx=winning_occ.cell_idx,
                        line_idx=winning_occ.line_idx,
                        level="warning"
                    )
                )

            flags_history = [tuple(h.flags) for h in history]
            if len(set(flags_history)) > 1:
                flags_display = " ".join(winning_occ.flags) if winning_occ.flags else "default index (no flags)"
                conflict_warnings.append(
                    DiagnosticEvent(
                        type="conflicting_flags",
                        detail=f"Conflicting Scoped Flags for '{winning_occ.name}': Overwriting earlier flags with '{flags_display}' ({time_qualifier}).",
                        cell_idx=winning_occ.cell_idx,
                        line_idx=winning_occ.line_idx,
                        level="warning"
                    )
                )

    return resolved, conflict_warnings


def harvest_scoped_cell_flags(code_sources: List[str]) -> Dict[str, List[str]]:
    """Convenience delegate returning harvested scoped flags map directly."""
    occurrences, _raw_installs = harvest_pip_install_occurrences(code_sources)
    resolved, _ = resolve_pip_occurrences(occurrences)
    return {pkg: occ.flags for pkg, occ in resolved.items()}


def harvest_index_urls_from_sources(code_sources: List[str]) -> Set[str]:
    """Scans code sources for index URLs and returns a combined set of all harvested URLs."""
    h_res = harvest_cell_magics_and_commands(code_sources)
    return h_res.base_index_urls.union(h_res.extra_index_urls)


def harvest_cell_magics_and_commands(
    code_sources: List[str]
) -> HarvestResult:
    """Scans code sources for cell magics, index URLs, auxiliary tools, and shell commands."""
    occurrences, raw_installs = harvest_pip_install_occurrences(code_sources)
    resolved_occs, magic_warnings = resolve_pip_occurrences(occurrences)

    harvested_packages: Set[str] = set()
    base_index_urls: Set[str] = set()
    extra_index_urls: Set[str] = set()
    magic_notices: List[DiagnosticEvent] = []
    scoped_flags: Dict[str, List[str]] = {}

    for raw_spec in raw_installs:
        magic_notices.append(
            DiagnosticEvent(
                type="raw_install",
                detail=f"'{raw_spec}' is installed from a non-standard source (git/URL/local file), not PyPI. "
                       f"It will still be installed exactly as specified, but can't be verified or checked for "
                       f"drift -- you're responsible for ensuring anyone running this notebook has access to "
                       f"the same resource.",
                cell_idx=0,
                line_idx=0,
                level="notice"
            )
        )

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

    for cell_idx, source in enumerate(code_sources, start=1):
        cell_type, clean_body = scanning.classify_cell_source(source)
        if cell_type == "WRITEFILE":
            continue

        for line_idx, line in enumerate(clean_body.splitlines()):
            clean_line = line.strip()
            if not clean_line or clean_line.startswith('#') or clean_line in SHELL_CELL_MAGICS:
                continue

            command_segments = SHELL_SPLIT_PATTERN.split(clean_line)
            for segment in command_segments:
                seg = segment.strip()
                if not seg:
                    continue

                if SYSTEM_PKG_PATTERN.match(seg):
                    magic_notices.append(
                        DiagnosticEvent(
                            type="system_command",
                            detail=f"Cell {cell_idx} uses a system install command ('{seg}'). Note: System dependencies must be run manually by readers.",
                            cell_idx=cell_idx - 1,
                            line_idx=line_idx,
                            level="notice"
                        )
                    )
                elif CONDA_INSTALL_PATTERN.match(seg):
                    magic_notices.append(
                        DiagnosticEvent(
                            type="conda_command",
                            detail=f"Cell {cell_idx} uses 'conda install'. Conda packages are not tracked in pip requirements manifests.",
                            cell_idx=cell_idx - 1,
                            line_idx=line_idx,
                            level="notice"
                        )
                    )
                elif PIP_INSTALL_PATTERN.match(seg):
                    if "-r " in seg or "--requirement" in seg:
                        magic_warnings.append(
                            DiagnosticEvent(
                                type="external_requirement",
                                detail=f"Cell {cell_idx} references an external requirements file ('{seg}'). Ensure that file is shared alongside your notebook.",
                                cell_idx=cell_idx - 1,
                                line_idx=line_idx,
                                level="warning"
                            )
                        )

    return HarvestResult(
        harvested_packages=harvested_packages,
        base_index_urls=base_index_urls,
        extra_index_urls=extra_index_urls,
        magic_warnings=magic_warnings,
        magic_notices=magic_notices,
        scoped_flags=scoped_flags,
        raw_installs=raw_installs
    )

