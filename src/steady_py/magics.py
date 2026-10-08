"""Cell magics and shell commands: pip, conda and system-package installs, index URLs, scoped pip
flags, and the auxiliary tools a notebook installs without importing."""
import ast
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from steady_py import scanning, util
from steady_py.constants import SHELL_CELL_MAGICS, GuardKind, Invocation
from steady_py.models import Cell, DiagnosticEvent, Guard, HarvestResult, InstallLine, PipInstallOccurrence


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


SHELL_JOIN = re.compile(r"&&|\|\|")
SHELL_BLOCK_OPEN = re.compile(r"^\s*(if|case|for|while|until)\b")
SHELL_BLOCK_CLOSE = re.compile(r"^\s*(fi|esac|done)\b")
INSTALL_MAGICS = {"pip", "conda"}


def _record(command: str, prefix: str, invocation: str, cell: Cell, cell_idx: int, line_idx: int,
            guard: Optional[Guard], found: _Harvest) -> None:
    """Records each install segment of one command. Shell-joined (`&&`, `||`) segments count as
    guarded (fix_plan.md, section 4, item 3); a guarded pip line contributes no pins (G3)."""
    if guard is None and invocation != Invocation.LINE_MAGIC and SHELL_JOIN.search(command):
        guard = Guard(GuardKind.SHELL_JOINED, command.strip(), scanning.guard_group(cell, line_idx + 1))
    for i, segment in enumerate(SHELL_SPLIT_PATTERN.split(command.strip())):
        seg = (prefix if i == 0 else "") + segment.strip()
        tool = _install_tool(seg)
        if tool is None:
            continue
        found.install_lines.append(InstallLine(text=seg, tool=tool, cell=cell, line_idx=line_idx,
                                               invocation=invocation, guard=guard))
        pip_match = PIP_INSTALL_PATTERN.match(seg)
        if tool == "pip" and pip_match and guard is None:
            _parse_pip_args(pip_match.group(1), cell_idx, cell, line_idx, found)


class _InstallCallVisitor(scanning.GuardTracker):
    """Install commands as IPython's transform writes them (`%pip` becomes
    `get_ipython().run_line_magic('pip', ...)`, `!cmd` becomes `get_ipython().system(...)`), each
    under the innermost enclosing guard."""
    def __init__(self, cell: Cell, cell_idx: int, found: _Harvest) -> None:
        super().__init__(cell)
        self.source_cell, self.cell_idx, self.found = cell, cell_idx, found
        self.raw_lines = cell.source.splitlines()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call)
                and isinstance(func.value.func, ast.Name) and func.value.func.id == "get_ipython"):
            args = [a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None for a in node.args]
            line_idx = node.lineno - 1
            guard = self.guards[-1] if self.guards else None
            if func.attr == "run_line_magic" and len(args) == 2 and args[0] in INSTALL_MAGICS and args[1] is not None:
                _record(f"{args[0]} {args[1]}", "%", Invocation.LINE_MAGIC, self.source_cell, self.cell_idx, line_idx, guard, self.found)
            elif func.attr in {"system", "getoutput"} and len(args) == 1 and args[0] is not None:
                raw = self.raw_lines[line_idx] if line_idx < len(self.raw_lines) else ""
                written = Invocation.PYTHON_CALL if "get_ipython()" in raw else Invocation.SHELL_ESCAPE
                _record(args[0], "" if written == Invocation.PYTHON_CALL else "!", written,
                        self.source_cell, self.cell_idx, line_idx, guard, self.found)
        self.generic_visit(node)


def _harvest_shell_body(text: str, first_line: int, cell: Cell, cell_idx: int, found: _Harvest) -> None:
    """A `%%bash`/`%%sh` body: lines inside a shell `if` or `case` block are guarded."""
    blocks: List[Tuple[str, int, str]] = []  # (keyword, line_idx, opening line)
    for line_idx, line in enumerate(text.splitlines(), start=first_line):
        clean_line = line.strip()
        if SHELL_BLOCK_CLOSE.match(clean_line) and blocks:
            blocks.pop()
        opening = SHELL_BLOCK_OPEN.match(clean_line)
        if opening:
            blocks.append((opening.group(1), line_idx, clean_line))
        if not clean_line or clean_line.startswith('#') or opening:
            continue
        conditional = [b for b in blocks if b[0] in {"if", "case"}]
        guard = (Guard(GuardKind.SHELL_CONDITIONAL, conditional[-1][2], scanning.guard_group(cell, conditional[-1][1] + 1))
                 if conditional else None)
        _record(clean_line, "", Invocation.SHELL_CELL, cell, cell_idx, line_idx, guard, found)


def _harvest_raw_lines(source: str, cell: Cell, cell_idx: int, found: _Harvest) -> None:
    """A cell that couldn't be parsed (already reported as unparseable_cell): line by line, with no
    Python guard information."""
    for line_idx, line in enumerate(source.splitlines()):
        clean_line = line.strip()
        if not clean_line or clean_line.startswith('#') or clean_line in SHELL_CELL_MAGICS:
            continue
        _record(clean_line, "", Invocation.SHELL_ESCAPE if clean_line.startswith("!") else Invocation.LINE_MAGIC,
                cell, cell_idx, line_idx, None, found)


def _harvest(code_sources: scanning.CellsLike) -> _Harvest:
    found = _Harvest()
    for cell_idx, cell in enumerate(scanning.as_cells(code_sources)):
        parsed = scanning.parse_cell(cell)
        if parsed.kind == "SHELL_SCRIPT":
            _harvest_shell_body(parsed.text, parsed.first_line, cell, cell_idx, found)
        elif parsed.kind == "PYTHON" and parsed.tree is not None:
            try:
                _InstallCallVisitor(cell, cell_idx, found).visit(parsed.tree)
            except RecursionError:
                pass  # reported by the import scan as cell_too_deep (K13); installs found so far are kept
        elif parsed.kind == "PYTHON":
            _harvest_raw_lines(cell.source, cell, cell_idx, found)
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
    Structured PipInstallOccurrence records for every unguarded pip install (G3), skipping %%writefile
    cells and cell magics whose body is neither Python nor shell.

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


def _guard_where(guard: Guard) -> str:
    if guard.kind == GuardKind.IF:
        if not guard.condition:
            return "in an `else` branch"
        return f"inside `{'if' if guard.branch == 0 else 'elif'} {guard.condition}`"
    if guard.kind == GuardKind.EXCEPT:
        return f"inside `except {guard.condition}`" if guard.condition else "inside an `except` block"
    if guard.kind == GuardKind.TRY:
        return "inside a `try` block"
    if guard.kind == GuardKind.FUNCTION:
        return f"inside function `{guard.condition}`, so only when it's called"
    if guard.kind == GuardKind.SHELL_JOINED:
        return "as part of a shell `&&`/`||` chain"
    return f"inside the shell block `{guard.condition}`"


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
        if line.tool == "pip" and line.guard is not None:
            magic_warnings.append(DiagnosticEvent.at(
                line.cell, line.line_idx, "guarded_install",
                f"'{line.text}' runs only {_guard_where(line.guard)}, so the setup cell doesn't install "
                f"its packages. The line stays in your notebook and runs there as before.",
            ))
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
