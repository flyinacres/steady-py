"""Cell magics and shell commands: pip, conda and system-package installs, index URLs, scoped pip
flags, and the auxiliary tools a notebook installs without importing.

Every install command becomes one InstallLine (fix_plan.md, section 3.1); pins, raw installs and
diagnostics are all derived from those records."""
import ast
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from steady_py import installargs, scanning, util
from steady_py.constants import (
    CARRIED_PIP_OPTIONS, PIP_OPTIONS, SHELL_CELL_MAGICS, TOOL_IMPORT_NAME, GuardKind, Invocation, Readability, TargetKind, Tool,
)
from steady_py.models import (
    Cell, DiagnosticEvent, Guard, HarvestResult, InstallLine, PipInstallOccurrence, RawInstall,
)

# Line magics that install packages, and the command each runs (`%uv pip install x` is `uv pip install x`).
INSTALL_MAGICS: Set[str] = {"pip", "uv", "conda", "mamba", "micromamba"}
_SUBPROCESS_CALLS = {"run", "call", "check_call", "check_output", "Popen"}
_LOOKS_LIKE_PIP_INSTALL = re.compile(r"\b(pip3?|uv\s+pip)\b.*\binstall\b")
_PIP_TOOLS = {Tool.PIP, Tool.UV}
_TOOL_DISTRIBUTION = util.canonicalize_pkg_name(TOOL_IMPORT_NAME)


@dataclass
class _Harvest:
    """Every install line, with the processing rank of its cell (for ordering against imports)."""
    lines: List[Tuple[int, InstallLine]] = field(default_factory=list)


@dataclass
class _ShellBlocks:
    """Open shell blocks across the lines of one `%%bash` cell, or within one `!` line."""
    stack: List[Tuple[str, int, str]] = field(default_factory=list)  # (keyword, line_idx, opening line)


def _record(command: str, text: str, invocation: str, cell: Cell, cell_idx: int, line_idx: int,
            guard: Optional[Guard], found: _Harvest, blocks: Optional[_ShellBlocks] = None) -> None:
    """Records each install command in one shell line. A command inside a shell `if` or `case`, or
    in a line joined with `&&`/`||`, counts as guarded (fix_plan.md, section 4, item 3)."""
    try:
        tokens = installargs.tokenize(command)
    except installargs.UnreadableCommand:
        if _LOOKS_LIKE_PIP_INSTALL.search(command):
            found.lines.append((cell_idx, InstallLine(text=text, tool=Tool.PIP, cell=cell, line_idx=line_idx,
                                                      invocation=invocation, guard=guard,
                                                      readability=Readability.UNREADABLE)))
        return
    blocks = blocks if blocks is not None else _ShellBlocks()
    joined = invocation != Invocation.LINE_MAGIC and any(t in installargs.JOINS for t in tokens)
    for segment in installargs.segments(tokens):
        if segment.closes and blocks.stack:
            blocks.stack.pop()
        if segment.opens:
            blocks.stack.append((segment.opens, line_idx, command.strip()))
        parsed = installargs.parse_command(segment.words)
        if parsed is None:
            continue
        found.lines.append((cell_idx, _line(parsed, text, invocation, cell, line_idx,
                                            _shell_guard(guard, blocks, joined, command, cell, line_idx))))


def _record_words(words: List[str], text: str, cell: Cell, cell_idx: int, line_idx: int,
                  guard: Optional[Guard], found: _Harvest) -> None:
    """An argument list run without a shell (a `subprocess` list)."""
    parsed = installargs.parse_command(words)
    if parsed is not None:
        found.lines.append((cell_idx, _line(parsed, text, Invocation.PYTHON_CALL, cell, line_idx, guard)))


def _shell_guard(guard: Optional[Guard], blocks: _ShellBlocks, joined: bool, command: str,
                 cell: Cell, line_idx: int) -> Optional[Guard]:
    if guard is not None:
        return guard
    conditional = [b for b in blocks.stack if b[0] in {"if", "case"}]
    if conditional:
        _, opened_at, opening = conditional[-1]
        return Guard(GuardKind.SHELL_CONDITIONAL, opening, scanning.guard_group(cell, opened_at + 1))
    if joined:
        return Guard(GuardKind.SHELL_JOINED, command.strip(), scanning.guard_group(cell, line_idx + 1))
    return None


def _line(parsed: installargs.ParsedCommand, text: str, invocation: str, cell: Cell, line_idx: int,
          guard: Optional[Guard]) -> InstallLine:
    return InstallLine(text=text, tool=parsed.tool, cell=cell, line_idx=line_idx, invocation=invocation,
                       guard=guard, readability=parsed.readability, kernel_target=not parsed.elsewhere,
                       elsewhere=parsed.elsewhere, targets=parsed.targets, options=parsed.options)


def _literal_text(node: ast.expr) -> Optional[str]:
    """A string argument as written: a literal, or an f-string with `{expr}` left in place, which
    readability then reports as computed. None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [v.value if isinstance(v, ast.Constant) else "{" + ast.unparse(v) + "}" for v in node.values]
        return "".join(str(p) for p in parts)
    return None


def _word(node: ast.expr) -> str:
    """One element of a `subprocess` argument list."""
    if ast.unparse(node) == "sys.executable":
        return "python"
    text = _literal_text(node)
    return text if text is not None else installargs.COMPUTED_MARK


def _is_get_ipython(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "get_ipython"


class _InstallCallVisitor(scanning.GuardTracker):
    """Install commands as IPython's transform writes them (`%pip` becomes
    `get_ipython().run_line_magic('pip', ...)`, `!cmd` becomes `get_ipython().system(...)`), and
    `os.system` and `subprocess` calls, each under the innermost enclosing guard."""
    def __init__(self, cell: Cell, cell_idx: int, found: _Harvest) -> None:
        super().__init__(cell)
        self.source_cell, self.cell_idx, self.found = cell, cell_idx, found
        self.raw_lines = cell.source.splitlines()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            owner = ("get_ipython()" if _is_get_ipython(func.value)
                     else func.value.id if isinstance(func.value, ast.Name) else "")
            self._install_call(owner, func.attr, node)
        self.generic_visit(node)

    def _install_call(self, owner: str, attr: str, node: ast.Call) -> None:
        line_idx = node.lineno - 1
        guard = self.guards[-1] if self.guards else None
        args = [_literal_text(a) for a in node.args]
        if owner == "get_ipython()" and attr == "run_line_magic" and len(args) == 2 \
                and args[0] in INSTALL_MAGICS and args[1] is not None:
            command = f"{args[0]} {args[1]}"
            _record(command, f"%{command}", Invocation.LINE_MAGIC, self.source_cell, self.cell_idx,
                    line_idx, guard, self.found)
        elif owner == "get_ipython()" and attr in {"system", "getoutput"} and len(args) == 1 and args[0] is not None:
            raw = self.raw_lines[line_idx] if line_idx < len(self.raw_lines) else ""
            if "get_ipython()" in raw:
                self._python_call(args[0], node, line_idx, guard)
            else:
                _record(args[0], f"!{args[0]}", Invocation.SHELL_ESCAPE, self.source_cell, self.cell_idx,
                        line_idx, guard, self.found)
        elif owner == "os" and attr == "system" and len(args) == 1 and args[0] is not None:
            self._python_call(args[0], node, line_idx, guard)
        elif owner == "subprocess" and attr in _SUBPROCESS_CALLS and node.args:
            first = node.args[0]
            if isinstance(first, (ast.List, ast.Tuple)):
                _record_words([_word(e) for e in first.elts], ast.unparse(node), self.source_cell,
                              self.cell_idx, line_idx, guard, self.found)
            elif args[0] is not None:
                self._python_call(args[0], node, line_idx, guard)

    def _python_call(self, command: str, node: ast.Call, line_idx: int, guard: Optional[Guard]) -> None:
        _record(command, ast.unparse(node), Invocation.PYTHON_CALL, self.source_cell, self.cell_idx,
                line_idx, guard, self.found)


def _harvest_shell_body(text: str, first_line: int, cell: Cell, cell_idx: int, found: _Harvest) -> None:
    """A `%%bash`/`%%sh` body, line by line; shell blocks carry across lines."""
    blocks = _ShellBlocks()
    for line_idx, line in enumerate(text.splitlines(), start=first_line):
        clean_line = line.strip()
        if clean_line:
            _record(clean_line, clean_line, Invocation.SHELL_CELL, cell, cell_idx, line_idx, None, found, blocks)


def _harvest_raw_lines(source: str, cell: Cell, cell_idx: int, found: _Harvest) -> None:
    """A cell that couldn't be parsed (already reported as unparseable_cell): line by line, with no
    Python guard information."""
    for line_idx, line in enumerate(source.splitlines()):
        clean_line = line.strip()
        if not clean_line or clean_line.startswith('#') or clean_line in SHELL_CELL_MAGICS:
            continue
        if clean_line.startswith("%"):
            magic, _, rest = clean_line[1:].partition(" ")
            if magic in INSTALL_MAGICS:
                _record(f"{magic} {rest}", clean_line, Invocation.LINE_MAGIC, cell, cell_idx, line_idx, None, found)
            continue
        _record(clean_line.lstrip("!"), clean_line, Invocation.SHELL_ESCAPE, cell, cell_idx, line_idx, None, found)


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


def _installs_into_kernel(line: InstallLine) -> bool:
    """True when the setup cell can reproduce this pip line: it runs unconditionally, into the
    kernel's environment, and pip would accept it."""
    return (line.tool in _PIP_TOOLS and line.guard is None and line.kernel_target
            and line.readability != Readability.UNREADABLE
            and not any(t.kind == TargetKind.INVALID for t in line.targets))


def carried_flags(line: InstallLine) -> List[str]:
    """The line's options that change what gets installed, as pip arguments (G14)."""
    return [arg for option in line.options if option.name in CARRIED_PIP_OPTIONS for arg in option.as_args()]


def _derive(found: _Harvest) -> Tuple[List[PipInstallOccurrence], List[Tuple[RawInstall, InstallLine]]]:
    """Pins and raw installs from the lines the setup cell reproduces."""
    occurrences: List[PipInstallOccurrence] = []
    raw_installs: List[Tuple[RawInstall, InstallLine]] = []
    for cell_idx, line in found.lines:
        if not _installs_into_kernel(line):
            continue
        flags = carried_flags(line)
        for target in line.targets:
            if target.kind == TargetKind.REQUIREMENT and target.canonical == _TOOL_DISTRIBUTION:
                continue  # the setup cell installing steady-py itself, as its import is skipped
            if target.kind == TargetKind.REQUIREMENT:
                occurrences.append(PipInstallOccurrence(
                    cell_idx=cell_idx, line_idx=line.line_idx, raw_token=target.text, name=target.name,
                    version_spec=target.specifier, flags=list(flags), cell=line.cell, extras=list(target.extras)))
            elif target.kind in {TargetKind.DIRECT_REFERENCE, TargetKind.URL, TargetKind.PATH}:
                raw_installs.append((RawInstall(target.text, tuple(flags)), line))
    return occurrences, raw_installs


def harvest_pip_install_occurrences(code_sources: scanning.CellsLike) -> Tuple[List[PipInstallOccurrence], List[RawInstall]]:
    """
    Structured PipInstallOccurrence records for every requirement the setup cell reproduces (G3),
    skipping %%writefile cells and cell magics whose body is neither Python nor shell.

    Also returns raw installs: URL, `name @ url` and local-path targets. These can't be decomposed
    into a name+version pin without running pip, so they're preserved as written, with their line's
    carried options.
    """
    occurrences, raw_installs = _derive(_harvest(code_sources))
    return occurrences, [raw for raw, _line in raw_installs]


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


def _index_urls(flags: List[str]) -> Tuple[Set[str], Set[str]]:
    """(base, extra) index URLs in a carried flag list."""
    base: Set[str] = set()
    extra: Set[str] = set()
    i = 0
    while i < len(flags):
        name, takes_value = PIP_OPTIONS.get(flags[i], (flags[i], False))
        value = flags[i + 1] if takes_value and i + 1 < len(flags) else ""
        if name == "--index-url":
            base.add(value)
        elif name in {"--extra-index-url", "--find-links"}:
            extra.add(value)
        i += 2 if takes_value else 1
    return base, extra


def _shared_file_warning(line: InstallLine, kind: str) -> DiagnosticEvent:
    what = "constraints" if kind == TargetKind.CONSTRAINTS_FILE else "requirements"
    return DiagnosticEvent.at(
        line.cell, line.line_idx, "external_requirement",
        f"References an external {what} file ('{line.text}'). Ensure that file is shared alongside your notebook.")


def _kaggle_input_notice(line: InstallLine, path: str, dataset: str) -> DiagnosticEvent:
    return DiagnosticEvent.at(
        line.cell, line.line_idx, "kaggle_input_install",
        f"'{path}' comes from the Kaggle dataset '{dataset}'. The notebook needs that dataset attached as an "
        f"input, and this install fails outside Kaggle.", level="notice")


def _raw_install_notice(line: InstallLine, spec: str) -> DiagnosticEvent:
    return DiagnosticEvent.at(
        line.cell, line.line_idx, "raw_install",
        f"'{spec}' is installed from a non-standard source (git/URL/local file), not PyPI. "
        f"It will still be installed exactly as specified, but can't be verified or checked for "
        f"drift -- you're responsible for ensuring anyone running this notebook has access to "
        f"the same resource.", level="notice")


def _pip_line_diagnostics(line: InstallLine) -> List[DiagnosticEvent]:
    """What a creator should know about one pip or uv line. A line the setup cell doesn't reproduce
    gets one warning saying why; its shared files and editables are reported either way."""
    out: List[DiagnosticEvent] = []
    for target in line.targets:
        if target.kind in {TargetKind.REQUIREMENTS_FILE, TargetKind.CONSTRAINTS_FILE}:
            out.append(_shared_file_warning(line, target.kind))
        elif target.kind == TargetKind.EDITABLE:
            out.append(DiagnosticEvent.at(
                line.cell, line.line_idx, "editable_install",
                f"'{line.text}' installs '{target.text}' in editable mode, so the setup cell doesn't install "
                f"it. Share that project with your notebook, or publish it and install it by name."))
    invalid = [t.text for t in line.targets if t.kind == TargetKind.INVALID]
    if line.guard is not None:
        out.append(DiagnosticEvent.at(
            line.cell, line.line_idx, "guarded_install",
            f"'{line.text}' runs only {_guard_where(line.guard)}, so the setup cell doesn't install "
            f"its packages. The line stays in your notebook and runs there as before."))
    elif line.readability == Readability.UNREADABLE:
        out.append(DiagnosticEvent.at(
            line.cell, line.line_idx, "unreadable_install",
            f"steady-py couldn't read '{line.text}' (a quote isn't closed), so the setup cell doesn't "
            f"install its packages."))
    elif not line.kernel_target:
        out.append(DiagnosticEvent.at(
            line.cell, line.line_idx, "other_environment_install",
            f"'{line.text}' installs somewhere other than the notebook's environment ({line.elsewhere}), so "
            f"the setup cell doesn't install its packages. The line stays in your notebook and runs there as before."))
    elif invalid:
        why = ("a direct reference needs quotes around it, as in \"name @ url\"" if "@" in invalid
               else f"'{invalid[0]}' needs a value" if invalid[0].startswith("-")
               else f"'{invalid[0]}' isn't a package, URL or path")
        out.append(DiagnosticEvent.at(
            line.cell, line.line_idx, "invalid_install",
            f"pip rejects '{line.text}' ({why}), so nothing on that line installs. Fix the line in your notebook."))
    elif line.readability == Readability.COMPUTED:
        out.append(DiagnosticEvent.at(
            line.cell, line.line_idx, "computed_install",
            f"'{line.text}' names packages or options with a variable, which steady-py can't know before it "
            f"runs, so the setup cell doesn't install those. The line stays in your notebook and runs there as before."))
    for option in line.options:
        dataset = installargs.kaggle_dataset(option.value or "")
        if dataset:
            out.append(_kaggle_input_notice(line, option.value or "", dataset))
    return out


def harvest_cell_magics_and_commands(
    code_sources: scanning.CellsLike
) -> HarvestResult:
    """Scans code sources for cell magics, index URLs, auxiliary tools, and shell commands."""
    found = _harvest(code_sources)
    occurrences, raw_installs = _derive(found)
    resolved_occs, magic_warnings = resolve_pip_occurrences(occurrences)
    magic_notices: List[DiagnosticEvent] = []

    for raw, line in raw_installs:
        dataset = installargs.kaggle_dataset(raw.spec)
        magic_notices.append(_kaggle_input_notice(line, raw.spec, dataset) if dataset
                             else _raw_install_notice(line, raw.spec))

    base_index_urls: Set[str] = set()
    extra_index_urls: Set[str] = set()
    scoped_flags: Dict[str, List[str]] = {}
    for occ in resolved_occs.values():
        scoped_flags[occ.name] = occ.flags
        base, extra = _index_urls(occ.flags)
        base_index_urls |= base
        extra_index_urls |= extra

    for _cell_idx, line in found.lines:
        if line.tool in _PIP_TOOLS:
            for event in _pip_line_diagnostics(line):
                (magic_notices if event.level == "notice" else magic_warnings).append(event)
        elif line.tool == Tool.SYSTEM:
            magic_notices.append(DiagnosticEvent.at(
                line.cell, line.line_idx, "system_command",
                f"Uses a system install command ('{line.text}'). Note: System dependencies must be run manually by readers.",
                level="notice",
            ))
        elif line.tool == Tool.CONDA:
            magic_notices.append(DiagnosticEvent.at(
                line.cell, line.line_idx, "conda_command",
                f"Uses a conda install command ('{line.text}'). Conda packages are not tracked in pip requirements manifests.",
                level="notice",
            ))
            magic_warnings.extend(_shared_file_warning(line, t.kind) for t in line.targets
                                  if t.kind == TargetKind.REQUIREMENTS_FILE)

    return HarvestResult(
        harvested_packages={occ.name for occ in occurrences},
        base_index_urls=base_index_urls,
        extra_index_urls=extra_index_urls,
        magic_warnings=magic_warnings,
        magic_notices=magic_notices,
        scoped_flags=scoped_flags,
        raw_installs=[raw for raw, _line in raw_installs],
        install_lines=[line for _cell_idx, line in found.lines],
    )
