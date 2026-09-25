"""Reading notebooks and live sessions: language detection, cell ordering and classification, and the
AST scan that finds imports, guarded imports and dynamic-import warnings."""
import ast
import json
import os
import re
import warnings
from typing import Any, Dict, List, Optional, Set, Tuple
from steady_py.constants import SHELL_CELL_MAGICS, StatusLabel
from steady_py.models import DiagnosticEvent, ExtractionResult, ImportOccurrence


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

    cells = nb_data.get("cells", [])
    ordered_cells, _ = get_ordered_code_cells(cells)
    code_sources = ["".join(c.get("source", [])) for _, c in ordered_cells]
    imports, submodules, guarded_imports, dyn_warnings, writefile_imports = extract_imports_from_sources_full(code_sources)

    return ExtractionResult(
        success=True,
        lang_label=lang_label,
        imports=imports,
        submodules=submodules,
        code_sources=code_sources,
        guarded_imports=guarded_imports,
        dynamic_warnings=dyn_warnings,
        writefile_imports=writefile_imports
    )


def extract_from_active_session() -> Tuple[List[str], Dict[str, Set[str]], List[str], Set[str], List[DiagnosticEvent]]:
    """
    Path B (Live Kernel): Reads IPython execution history in chronological order.
    Filters out self-referential steady_py execution cells and invocation commands.
    """
    import __main__
    raw_sources = [src for src in getattr(__main__, 'In', []) if src and isinstance(src, str)]
    
    clean_sources: List[str] = []
    for src in raw_sources:
        if "NotebookImportVisitor" in src or "def extract_from_active_session" in src:
            continue
        stripped = src.strip()
        if re.search(r'\b(?:spy|steady_py|steady_py\.cli)\.main\s*\(', stripped) or stripped in ("import steady_py", "import steady_py.cli") or stripped.startswith(("import steady_py as", "import steady_py.cli as")):
            continue
        clean_sources.append(src)

    imports, submodules, guarded_imports, dyn_warnings = extract_imports_from_sources_typed(clean_sources)
    return imports, submodules, clean_sources, guarded_imports, dyn_warnings


# =====================================================================
# AST VISITOR & DYNAMIC IMPORT PARSER
# =====================================================================

class NotebookImportVisitor(ast.NodeVisitor):
    """AST visitor traversing Python code to record imports, guarded states, and dynamic calls in order."""
    def __init__(self, cell_idx: int = 0) -> None:
        self.cell_idx: int = cell_idx
        self.imports: List[str] = []
        self.writefile_imports: List[str] = []
        self.submodules: Dict[str, Set[str]] = {}
        self.unconditional_imports: Set[str] = set()
        self.raw_guarded_imports: Set[str] = set()
        self.dynamic_import_warnings: List[DiagnosticEvent] = []
        self.occurrences: List[ImportOccurrence] = []
        self._guarded_depth: int = 0
        self._in_writefile: bool = False

        self._importlib_aliases: Set[str] = {"importlib"}
        self._import_module_bindings: Set[str] = set()

    @property
    def guarded_imports(self) -> Set[str]:
        return self.raw_guarded_imports - self.unconditional_imports

    def _record_import(self, base_pkg: str, full_name: Optional[str] = None, lineno: int = 1) -> None:
        line_idx = max(0, lineno - 1)
        if self._in_writefile:
            if base_pkg not in self.writefile_imports:
                self.writefile_imports.append(base_pkg)
            return

        if base_pkg not in self.imports:
            self.imports.append(base_pkg)

        is_guarded = self._guarded_depth > 0
        if is_guarded:
            self.raw_guarded_imports.add(base_pkg)
        else:
            self.unconditional_imports.add(base_pkg)

        if full_name and '.' in full_name:
            self.submodules.setdefault(base_pkg, set()).add(full_name)

        self.occurrences.append(
            ImportOccurrence(
                cell_idx=self.cell_idx,
                line_idx=line_idx,
                module=base_pkg,
                full_name=full_name or base_pkg,
                is_guarded=is_guarded
            )
        )

    def visit_Try(self, node: ast.Try) -> None:
        self._guarded_depth += 1
        self.generic_visit(node)
        self._guarded_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        self._guarded_depth += 1
        self.generic_visit(node)
        self._guarded_depth -= 1

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
                self.dynamic_import_warnings.append(
                    DiagnosticEvent(
                        type="dynamic_import",
                        detail=f"Dynamic import detected via variable '{expr_repr}'. Check that this package is installed if execution fails.",
                        cell_idx=self.cell_idx,
                        line_idx=getattr(node, "lineno", 1) - 1,
                        level="warning"
                    )
                )

        self.generic_visit(node)


def extract_import_occurrences_from_source(source: str, cell_idx: int = 0) -> List[ImportOccurrence]:
    """
    Parses an individual cell source using blank-line padding for stripped magics
    so that AST lineno perfectly matches raw cell line numbers.
    """
    cell_type, clean_body = classify_cell_source(source)
    if cell_type in {"SHELL_SCRIPT", "WRITEFILE"}:
        return []

    clean_lines = [
        "" if (line.strip().startswith('%') or line.strip().startswith('!')) else line
        for line in clean_body.splitlines()
    ]
    clean_source = "\n".join(clean_lines)

    visitor = NotebookImportVisitor(cell_idx=cell_idx)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=SyntaxWarning)
            tree = ast.parse(clean_source)
        visitor.visit(tree)
    except SyntaxError:
        return []

    return visitor.occurrences


def extract_imports_from_sources_full(
    code_sources: List[str]
) -> Tuple[List[str], Dict[str, Set[str]], Set[str], List[DiagnosticEvent], List[str]]:
    """Executes single-pass AST traversal returning primary and writefile imports with typed diagnostics."""
    visitor = NotebookImportVisitor()
    for cell_idx, source in enumerate(code_sources):
        visitor.cell_idx = cell_idx
        cell_type, clean_body = classify_cell_source(source)

        if cell_type == "SHELL_SCRIPT":
            continue

        visitor._in_writefile = (cell_type == "WRITEFILE")

        clean_lines = [
            "" if (line.strip().startswith('%') or line.strip().startswith('!')) else line
            for line in clean_body.splitlines()
        ]
        clean_source = "\n".join(clean_lines)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=SyntaxWarning)
                tree = ast.parse(clean_source)
            visitor.visit(tree)
        except SyntaxError:
            continue

    primary_imports = [imp for imp in visitor.imports if imp not in visitor.writefile_imports]
    return (
        primary_imports, 
        visitor.submodules, 
        visitor.guarded_imports, 
        visitor.dynamic_import_warnings,
        visitor.writefile_imports
    )


def extract_imports_from_sources(
    code_sources: List[str]
) -> Tuple[List[str], Dict[str, Set[str]], Set[str], List[str]]:
    """Legacy 4-tuple extractor for primary imports with formatted strings."""
    primary_imports, submodules, guarded, dyn_warns, _ = extract_imports_from_sources_full(code_sources)
    return primary_imports, submodules, guarded, [w.format_console() for w in dyn_warns]


def extract_imports_from_sources_typed(
    code_sources: List[str]
) -> Tuple[List[str], Dict[str, Set[str]], Set[str], List[DiagnosticEvent]]:
    """Typed 4-tuple extractor for primary imports returning DiagnosticEvent objects."""
    primary_imports, submodules, guarded, dyn_warns, _ = extract_imports_from_sources_full(code_sources)
    return primary_imports, submodules, guarded, dyn_warns


def extract_writefile_imports_from_sources(code_sources: List[str]) -> List[str]:
    """Extracts writefile script imports."""
    _, _, _, _, writefile_imports = extract_imports_from_sources_full(code_sources)
    return writefile_imports



def classify_cell_source(source: str) -> Tuple[str, str]:
    """Classifies cell source into (cell_type, clean_source)."""
    lines = source.splitlines()
    if not lines:
        return "PYTHON", ""

    first_line = lines[0].strip()
    first_token = first_line.split()[0] if first_line.split() else ""

    if first_token in SHELL_CELL_MAGICS:
        return "SHELL_SCRIPT", "\n".join(lines[1:])

    if first_token == "%%writefile":
        return "WRITEFILE", "\n".join(lines[1:])

    return "PYTHON", source

