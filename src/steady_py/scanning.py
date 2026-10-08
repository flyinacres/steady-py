"""Reading notebooks and live sessions: language detection, cell ordering and classification, and the
AST scan that finds imports, guarded imports and dynamic-import warnings."""
import ast
import json
import os
import re
import sys
import warnings
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union
from IPython.core.inputtransformer2 import TransformerManager
from packaging.version import InvalidVersion, Version

from steady_py.constants import PYTHON_CELL_MAGICS, SHELL_CELL_MAGICS, TOOL_IMPORT_NAME, GuardKind, StatusLabel
from steady_py.models import Cell, DiagnosticEvent, ExtractionResult, Guard, ImportOccurrence

CellsLike = Sequence[Union[str, Cell]]


def as_cells(items: CellsLike) -> List[Cell]:
    """Cells in processing order. Bare strings (older callers and tests) become cells positioned by
    their index in the list."""
    return [item if isinstance(item, Cell) else Cell(source=item, position=i) for i, item in enumerate(items)]


def _cell_text(value: Any) -> str:
    return "".join(value) if isinstance(value, list) else (value if isinstance(value, str) else "")


def _heading(markdown: str) -> Optional[str]:
    """The last heading line in a markdown cell, without its #s."""
    found = [line.lstrip("#").strip() for line in markdown.splitlines() if line.startswith("#")]
    return found[-1] if found else None


def read_notebook_cells(nb_cells: List[Dict[str, Any]]) -> List[Cell]:
    """A notebook's code cells in processing order, each with its notebook position and heading."""
    headings: Dict[int, Optional[str]] = {}
    current: Optional[str] = None
    for idx, cell in enumerate(nb_cells):
        headings[idx] = current
        if cell.get("cell_type") == "markdown":
            current = _heading(_cell_text(cell.get("source"))) or current
    ordered, _ = get_ordered_code_cells(nb_cells)
    return [
        Cell(source=_cell_text(c.get("source")), position=idx,
             execution_count=c.get("execution_count") if isinstance(c.get("execution_count"), int) else None,
             heading=headings[idx])
        for idx, c in ordered
    ]


def read_session_cells() -> List[Cell]:
    """The live IPython session's cells as typed (`input_hist_raw`), numbered by history index."""
    try:
        from IPython import get_ipython
    except ImportError:
        return []
    shell = get_ipython()
    if shell is None:
        return []
    raw: List[str] = list(shell.history_manager.input_hist_raw)
    return [Cell(source=src, execution_count=i) for i, src in enumerate(raw) if i > 0 and src.strip()]


def extract_from_cells(cells: List[Cell], lang_label: str = StatusLabel.PYTHON,
                       notebook_python: Optional[str] = None) -> ExtractionResult:
    """The one analysis of read cells, shared by file and live mode. `notebook_python` is the Python
    version the notebook records, if any."""
    imports, submodules, guarded_imports, dyn_warnings, writefile_imports = extract_imports_from_sources_full(
        cells, notebook_python)
    return ExtractionResult(
        success=True,
        lang_label=lang_label,
        imports=imports,
        submodules=submodules,
        cells=cells,
        guarded_imports=guarded_imports,
        dynamic_warnings=dyn_warnings,
        writefile_imports=writefile_imports,
    )


def get_timeline_context_label(is_execution_ordered: bool) -> str:
    """Returns the standardized authority qualifier for timeline-dependent diagnostics."""
    if is_execution_ordered:
        return "in execution sequence"
    return "in document order (execution counts unavailable or inconsistent)"


def get_ordered_code_cells(cells: List[Dict[str, Any]]) -> Tuple[List[Tuple[int, Dict[str, Any]]], bool]:
    """
    Evaluates execution_count across all code cells.
    If 100% of code cells have valid, unique positive integer execution counts,
    orders cells strictly by execution_count ascending.
    Otherwise, falls back 100% to document index order.
    """
    code_cells = [(idx, c) for idx, c in enumerate(cells) if c.get("cell_type") == "code"]
    if not code_cells:
        return [], False

    counts = [c.get("execution_count") for _, c in code_cells]
    
    is_fully_ordered = (
        all(isinstance(cnt, int) and cnt > 0 for cnt in counts)
        and len(set(counts)) == len(counts)
    )

    if is_fully_ordered:
        ordered = sorted(code_cells, key=lambda pair: pair[1]["execution_count"])
        return ordered, True

    return code_cells, False



def detect_notebook_language(nb_data: Dict[str, Any], strict: bool = False) -> Tuple[bool, str]:
    """Inspects kernelspec and language_info metadata."""
    metadata = nb_data.get("metadata", {})
    ks_lang = metadata.get("kernelspec", {}).get("language", "").lower()
    li_lang = metadata.get("language_info", {}).get("name", "").lower()

    if ks_lang and li_lang:
        if ks_lang == li_lang:
            return (ks_lang == StatusLabel.PYTHON), ks_lang
        else:
            return False, f"conflict ({ks_lang}/{li_lang})"
    
    active_lang = ks_lang or li_lang
    if active_lang:
        return (active_lang == StatusLabel.PYTHON), active_lang
        
    return True, "unspecified (assuming python)"


def extract_from_file(
    notebook_path: str, strict: bool = False
) -> ExtractionResult:
    """Reads a Jupyter Notebook JSON file and extracts code sources, imports, guarded state, and dynamic warnings."""
    if not os.path.exists(notebook_path):
        return ExtractionResult(
            success=False,
            lang_label=StatusLabel.UNKNOWN,
            error_msg=f"File '{notebook_path}' not found."
        )

    try:
        with open(notebook_path, 'r', encoding='utf-8') as f:
            nb_data = json.load(f)
    except json.JSONDecodeError:
        return ExtractionResult(
            success=False,
            lang_label=StatusLabel.CORRUPTED,
            error_msg="File is not valid JSON. Ensure the file was not truncated or saved mid-write."
        )
    except Exception as e:
        return ExtractionResult(
            success=False,
            lang_label=StatusLabel.ERROR,
            error_msg=f"Unable to read file ({type(e).__name__}). Check file permissions and path location."
        )

    if not isinstance(nb_data, dict) or "cells" not in nb_data or not isinstance(nb_data.get("cells"), list):
        return ExtractionResult(
            success=False,
            lang_label=StatusLabel.CORRUPTED,
            error_msg="Unparseable notebook structure (Missing or invalid 'cells' array)"
        )

    is_py, lang_label = detect_notebook_language(nb_data, strict=strict)
    if not is_py:
        return ExtractionResult(
            success=False,
            lang_label=lang_label,
            error_msg=f"Skipped non-Python notebook (Language: {lang_label})"
        )

    language_info = nb_data.get("metadata", {}).get("language_info", {})
    recorded = language_info.get("version") if isinstance(language_info, dict) else None
    return extract_from_cells(read_notebook_cells(nb_data["cells"]), lang_label,
                              recorded if isinstance(recorded, str) else None)


def extract_from_active_session() -> ExtractionResult:
    """Live kernel: the session's cells as typed, in execution order."""
    return extract_from_cells(read_session_cells())


# =====================================================================
# AST VISITOR & DYNAMIC IMPORT PARSER
# =====================================================================

def guarded_modules(occurrences: Sequence[ImportOccurrence]) -> Set[str]:
    """Modules imported only under a guard. One unconditional import anywhere makes a module
    unconditional: `try: import x / except: !pip install x` followed by `import x` needs x (G20)."""
    unconditional = {o.module for o in occurrences if not o.is_guarded}
    return {o.module for o in occurrences if o.is_guarded} - unconditional


def guard_group(cell: Optional[Cell], line: int) -> str:
    """A guard's identity: its cell (notebook position, or In [n] in a live session) and 1-based line."""
    where = cell.position if cell and cell.position is not None else f"In{cell.execution_count if cell else ''}"
    return f"{where}:{line}"


class GuardTracker(ast.NodeVisitor):
    """The one rule for what a statement runs under: `guards` holds the enclosing if/elif/else,
    try/except and function-body clauses, outermost first, while the walk is inside them.

    Install lines count every enclosing guard, function bodies included (G9: a call site isn't
    traced). Imports don't count function bodies, since a helper's import is needed whenever it's
    called."""
    def __init__(self, cell: Optional[Cell] = None) -> None:
        self.cell: Optional[Cell] = cell
        self.guards: List[Guard] = []

    def _group(self, node: ast.stmt) -> str:
        return guard_group(self.cell, node.lineno)

    def _under(self, guard: Guard, body: Sequence[ast.AST]) -> None:
        self.guards.append(guard)
        try:
            for child in body:
                self.visit(child)
        finally:
            self.guards.pop()

    def visit_If(self, node: ast.If) -> None:
        self._visit_if(node, self._group(node), 0)

    def _visit_if(self, node: ast.If, group: str, branch: int) -> None:
        self.visit(node.test)
        self._under(Guard(GuardKind.IF, ast.unparse(node.test), group, branch), node.body)
        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            self._visit_if(node.orelse[0], group, branch + 1)  # elif
        elif node.orelse:
            self._under(Guard(GuardKind.IF, "", group, branch + 1), node.orelse)

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_try(node)

    def visit_TryStar(self, node: ast.AST) -> None:  # Python 3.11+
        self._visit_try(node)

    def _visit_try(self, node: Any) -> None:
        group = self._group(node)
        self._under(Guard(GuardKind.TRY, "", group, 0), [*node.body, *node.orelse, *node.finalbody])
        for i, handler in enumerate(node.handlers, start=1):
            condition = ast.unparse(handler.type) if handler.type else ""
            self._under(Guard(GuardKind.EXCEPT, condition, group, i), handler.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> None:
        for child in (*node.decorator_list, node.args):
            self.visit(child)
        self._under(Guard(GuardKind.FUNCTION, node.name, self._group(node), 0), node.body)


class NotebookImportVisitor(GuardTracker):
    """AST visitor traversing Python code to record imports, guarded states, and dynamic calls in order."""
    def __init__(self, cell_idx: int = 0, cell: Optional[Cell] = None) -> None:
        super().__init__(cell)
        self.cell_idx: int = cell_idx  # processing rank, for ordering
        self.imports: List[str] = []
        self.writefile_imports: List[str] = []
        self.submodules: Dict[str, Set[str]] = {}
        self.diagnostics: List[DiagnosticEvent] = []
        self.occurrences: List[ImportOccurrence] = []
        self._in_writefile: bool = False

        self._importlib_aliases: Set[str] = {"importlib"}
        self._import_module_bindings: Set[str] = set()

    @property
    def guarded_imports(self) -> Set[str]:
        return guarded_modules(self.occurrences)

    def _record_import(self, base_pkg: str, full_name: Optional[str] = None, lineno: int = 1) -> None:
        if base_pkg == TOOL_IMPORT_NAME:
            return  # steady-py is the tool, never a dependency; Cell 2 installs it (D1)
        line_idx = max(0, lineno - 1)
        if self._in_writefile:
            if base_pkg not in self.writefile_imports:
                self.writefile_imports.append(base_pkg)
            return

        if base_pkg not in self.imports:
            self.imports.append(base_pkg)

        is_guarded = any(g.kind != GuardKind.FUNCTION for g in self.guards)

        if full_name and '.' in full_name:
            self.submodules.setdefault(base_pkg, set()).add(full_name)

        self.occurrences.append(
            ImportOccurrence(
                cell_idx=self.cell_idx,
                line_idx=line_idx,
                module=base_pkg,
                full_name=full_name or base_pkg,
                is_guarded=is_guarded,
                cell=self.cell,
            )
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            base_pkg = alias.name.split('.')[0]
            if alias.name == "importlib":
                self._importlib_aliases.add(alias.asname or "importlib")
            self._record_import(base_pkg, full_name=alias.name, lineno=node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            base_pkg = node.module.split('.')[0]
            if node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        self._import_module_bindings.add(alias.asname or "import_module")
            self._record_import(base_pkg, full_name=node.module, lineno=node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        is_dynamic_import = False

        if isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id in self._importlib_aliases:
                if node.func.attr == "import_module":
                    is_dynamic_import = True

        elif isinstance(node.func, ast.Name):
            if node.func.id == "__import__" or node.func.id in self._import_module_bindings:
                is_dynamic_import = True

        if is_dynamic_import and node.args:
            first_arg = node.args[0]

            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                imported_pkg = first_arg.value
                base_pkg = imported_pkg.split('.')[0]
                self._record_import(base_pkg, full_name=imported_pkg, lineno=node.lineno)
            else:
                expr_repr = ast.unparse(first_arg) if hasattr(ast, "unparse") else "expression"
                self.diagnostics.append(
                    DiagnosticEvent.at(
                        self.cell, getattr(node, "lineno", 1) - 1, "dynamic_import",
                        f"Dynamic import detected via variable '{expr_repr}'. Check that this package is installed if execution fails.",
                    )
                )

        self.generic_visit(node)


# =====================================================================
# CELL PARSING: IPython's transform, then one AST per cell
# =====================================================================

_TRANSFORMER = TransformerManager()  # type: ignore[no-untyped-call]


@dataclass
class ParsedCell:
    """A cell as Python sees it after IPython's transform.

    `kind` is PYTHON, WRITEFILE, SHELL_SCRIPT or OTHER_MAGIC (a cell magic whose body isn't Python).
    `tree` has line numbers on the cell's own lines (1-based). It is None when there is nothing to
    analyze or the cell couldn't be parsed; `diagnostic` then says why, if the user should know.
    For a cell magic whose body isn't Python, `text` is the raw body and `first_line` the 0-based
    cell line it starts on.
    """
    kind: str
    tree: Optional[ast.Module] = None
    diagnostic: Optional[DiagnosticEvent] = None
    text: str = ""
    first_line: int = 0


def _leading_blank_lines(text: str) -> int:
    """Lines IPython's transform strips from the top of a cell (leading_empty_lines)."""
    count = 0
    for line in text.splitlines(keepends=True):
        if line and not line.isspace():
            break
        count += 1
    return count


def _run_cell_magic(transformed: str) -> Optional[Tuple[str, str]]:
    """(magic name, body) when the transform turned the cell into one run_cell_magic call."""
    if not transformed.startswith("get_ipython().run_cell_magic("):
        return None
    try:
        tree = ast.parse(transformed)
    except SyntaxError:
        return None
    call = tree.body[0].value if len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr) else None
    if not isinstance(call, ast.Call) or len(call.args) != 3:
        return None
    name, _args, body = (a.value if isinstance(a, ast.Constant) else None for a in call.args)
    return (name, body) if isinstance(name, str) and isinstance(body, str) else None


def _split_cell(source: str) -> Tuple[str, str, int]:
    """(kind, text, line offset). A cell magic gives its raw body and the cell line it starts on; a
    Python-body magic gives kind PYTHON_MAGIC. Any other cell gives the transformed source, whose
    line N is cell line N + offset."""
    transformed = _TRANSFORMER.transform_cell(source)
    lead = _leading_blank_lines(source)
    magic = _run_cell_magic(transformed)
    if magic is None:
        return "PYTHON", transformed, lead - _leading_blank_lines(transformed)
    name, body = magic
    if f"%%{name}" in SHELL_CELL_MAGICS:
        kind = "SHELL_SCRIPT"
    elif name == "writefile":
        kind = "WRITEFILE"
    elif name in PYTHON_CELL_MAGICS:
        kind = "PYTHON_MAGIC"
    else:
        kind = "OTHER_MAGIC"
    return kind, body, lead + 1


def classify_cell_source(source: str) -> Tuple[str, str, int]:
    """(kind, raw text, line offset) for line-based readers: a cell magic's body and the cell line it
    starts on, or the whole cell. A Python-body magic's body is PYTHON."""
    kind, text, offset = _split_cell(source)
    if kind == "PYTHON":
        return kind, source, 0
    return ("PYTHON" if kind == "PYTHON_MAGIC" else kind), text, offset


def _version_note(notebook_python: Optional[str]) -> str:
    """Names both versions when the notebook records a newer Python than the one running steady-py."""
    try:
        recorded = Version(notebook_python).release[:2] if notebook_python else None
    except InvalidVersion:
        recorded = None
    if recorded is None or recorded <= sys.version_info[:2]:
        return ""
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    return (f" The notebook records Python {notebook_python}, newer than the Python {running} running"
            f" steady-py; run steady-py with Python {recorded[0]}.{recorded[1]} or later.")


def _too_deep(cell: Optional[Cell]) -> DiagnosticEvent:
    return DiagnosticEvent.at(cell, None, "cell_too_deep",
                              "This cell nests too deeply for steady-py to analyze, so imports in it may be missing from the report.")


_CONTINUED_ESCAPE = re.compile(r"^\s*(?:[\w.,\s]+=\s*)?[%!].*\\$")


def _line_starts(source: str, transformed: str, offset: int) -> Optional[List[int]]:
    """0-based raw line of each transformed line (K14). IPython joins a `%` or `!` line ending in a
    backslash with the lines it continues, and nothing else, so each joined run counts once. None if
    the count doesn't match the transform, so the plain offset applies."""
    raw = source.splitlines()
    starts: List[int] = []
    i = 0
    while i < len(raw):
        starts.append(i)
        if _CONTINUED_ESCAPE.match(raw[i]):
            while i + 1 < len(raw) and raw[i].rstrip().endswith("\\"):
                i += 1
        i += 1
    if starts == list(range(len(raw))) or len(starts) - offset != len(transformed.splitlines()):
        return None
    return starts


def _raw_line(starts: Optional[List[int]], lineno: int, offset: int) -> int:
    """1-based raw line for a 1-based line of the transformed text."""
    if starts is None or not 0 <= lineno - 1 + offset < len(starts):
        return lineno + offset
    return starts[lineno - 1 + offset] + 1


def parse_cell(cell: Cell, notebook_python: Optional[str] = None) -> ParsedCell:
    """The one place that decides how a cell reads as Python (K1, K2, G13)."""
    return _parse(cell.source, 0, cell, notebook_python)


def _parse(source: str, offset: int, cell: Cell, notebook_python: Optional[str]) -> ParsedCell:
    kind, text, line = _split_cell(source)
    if kind == "PYTHON_MAGIC":
        return _parse(text, offset + line, cell, notebook_python)
    if kind not in {"PYTHON", "WRITEFILE"}:
        return ParsedCell(kind, text=text, first_line=offset + line)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=SyntaxWarning)
            tree = ast.parse(text)
    except SyntaxError as e:
        if kind == "WRITEFILE":
            return ParsedCell(kind)  # a written file needn't be Python
        error_line = offset + (_raw_line(_line_starts(source, text, line), e.lineno or 1, line) if kind == "PYTHON"
                               else (e.lineno or 1) + line)
        where = f"line {error_line}"
        return ParsedCell(kind, diagnostic=DiagnosticEvent.at(
            cell, error_line - 1, "unparseable_cell",
            f"steady-py couldn't read this cell as Python ({where}: {e.msg}), so imports in it are missing"
            f" from the report.{_version_note(notebook_python)}"))
    except RecursionError:
        return ParsedCell(kind, diagnostic=_too_deep(cell))
    starts = _line_starts(source, text, line) if kind == "PYTHON" else None
    if starts is None:
        ast.increment_lineno(tree, offset + line)
    else:
        for node in ast.walk(tree):
            for attr in ("lineno", "end_lineno"):
                if getattr(node, attr, None) is not None:
                    setattr(node, attr, offset + _raw_line(starts, getattr(node, attr), line))
    return ParsedCell(kind, tree)


def visit_cell(visitor: "NotebookImportVisitor", parsed: ParsedCell) -> Optional[DiagnosticEvent]:
    """Runs the import visitor over one parsed cell. A cell nested deeper than the recursion limit is
    reported and skipped; imports found before the overflow are kept (K13)."""
    if parsed.tree is None:
        return parsed.diagnostic
    visitor._in_writefile = parsed.kind == "WRITEFILE"
    try:
        visitor.visit(parsed.tree)
    except RecursionError:
        visitor.guards.clear()
        return _too_deep(visitor.cell)
    finally:
        visitor._in_writefile = False
    return None


def extract_import_occurrences_from_source(source: str, cell_idx: int = 0, cell: Optional[Cell] = None) -> List[ImportOccurrence]:
    """Import occurrences of one cell, at their line in the cell. Diagnostics are reported by
    extract_imports_from_sources_full."""
    cell = cell or Cell(source=source, position=cell_idx)
    visitor = NotebookImportVisitor(cell_idx=cell_idx, cell=cell)
    visit_cell(visitor, parse_cell(cell))
    return visitor.occurrences


def extract_imports_from_sources_full(
    code_sources: CellsLike, notebook_python: Optional[str] = None
) -> Tuple[List[str], Dict[str, Set[str]], Set[str], List[DiagnosticEvent], List[str]]:
    """One AST pass per cell: primary and writefile imports, guarded imports and diagnostics."""
    visitor = NotebookImportVisitor()
    for cell_idx, cell in enumerate(as_cells(code_sources)):
        visitor.cell_idx = cell_idx
        visitor.cell = cell
        diagnostic = visit_cell(visitor, parse_cell(cell, notebook_python))
        if diagnostic is not None:
            visitor.diagnostics.append(diagnostic)

    primary_imports = [imp for imp in visitor.imports if imp not in visitor.writefile_imports]
    return (
        primary_imports,
        visitor.submodules,
        visitor.guarded_imports,
        visitor.diagnostics,
        visitor.writefile_imports
    )


def extract_imports_from_sources(
    code_sources: CellsLike
) -> Tuple[List[str], Dict[str, Set[str]], Set[str], List[str]]:
    """Legacy 4-tuple extractor for primary imports with formatted strings."""
    primary_imports, submodules, guarded, dyn_warns, _ = extract_imports_from_sources_full(code_sources)
    return primary_imports, submodules, guarded, [w.format_console() for w in dyn_warns]


def extract_imports_from_sources_typed(
    code_sources: CellsLike
) -> Tuple[List[str], Dict[str, Set[str]], Set[str], List[DiagnosticEvent]]:
    """Typed 4-tuple extractor for primary imports returning DiagnosticEvent objects."""
    primary_imports, submodules, guarded, dyn_warns, _ = extract_imports_from_sources_full(code_sources)
    return primary_imports, submodules, guarded, dyn_warns


def extract_writefile_imports_from_sources(code_sources: CellsLike) -> List[str]:
    """Extracts writefile script imports."""
    _, _, _, _, writefile_imports = extract_imports_from_sources_full(code_sources)
    return writefile_imports
