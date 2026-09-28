"""Importing steady_py must not touch process-global state; only the CLI entry point may.
Also pins that best-effort probes stay quiet by default but leave a trace under --verbose."""
import ast
import importlib
import importlib.util
import inspect
import logging
import os
import subprocess
import sys
from pathlib import Path

import steady_py
from steady_py import resolution

SRC_DIR = str(Path(steady_py.__file__).resolve().parents[1])


def _run(code: str, **env) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=SRC_DIR,
        env={**os.environ, **env},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_import_leaves_streams_and_logging_untouched():
    out = _run(
        "import sys, logging\n"
        "before = sys.stdout.encoding\n"
        "import steady_py\n"
        "log = logging.getLogger('steady_py')\n"
        "print(sys.stdout.encoding == before, [type(h).__name__ for h in log.handlers], log.propagate)",
        PYTHONIOENCODING="latin-1",
    )
    assert out == "True ['NullHandler'] True"


def test_library_messages_reach_a_hosts_root_logger():
    """H3: a host that configures only the root logger (a library user, a notebook) gets them."""
    out = _run(
        "import logging, steady_py\n"
        "logging.basicConfig(level=logging.INFO, format='%(name)s %(message)s', stream=__import__('sys').stdout)\n"
        "logging.getLogger('steady_py.anymodule').warning('hello')"
    )
    assert out == "steady_py.anymodule hello"


def testconfigure_console_is_where_streams_and_the_handler_get_set_up():
    """The stderr handler replaces the import-time placeholder, leaving exactly one handler, and
    propagation stops so a host's root handler doesn't print each message twice."""
    out = _run(
        "import sys, logging, steady_py.cli as cli\n"
        "cli.configure_console()\n"
        "cli.configure_console()  # idempotent\n"
        "log = logging.getLogger('steady_py')\n"
        "print(sys.stdout.encoding, sorted(type(h).__name__ for h in log.handlers), log.propagate)",
        PYTHONIOENCODING="latin-1",
    )
    assert out == "utf-8 ['StreamHandler'] False"


def test_failed_opencv_probe_falls_back_and_is_logged_at_debug(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise OSError("pip is unavailable")

    monkeypatch.setattr(resolution.subprocess, "run", boom)
    caplog.set_level(logging.DEBUG, logger="steady_py")
    assert resolution.resolve_opencv_variant() == "opencv-python"
    assert "Could not inspect installed OpenCV variants" in caplog.text


def test_reloading_the_module_in_one_process_never_stacks_handlers():
    """Reloading the package in a live kernel (autoreload, or a re-import after an edit) re-runs its __init__."""
    out = _run(
        "import importlib, logging, steady_py, steady_py.cli as cli\n"
        "log = logging.getLogger('steady_py')\n"
        "for _ in range(2):\n"
        "    importlib.reload(steady_py)\n"
        "placeholders = len(log.handlers)\n"
        "for _ in range(2):\n"
        "    importlib.reload(steady_py)\n"
        "    cli.configure_console()\n"
        "print(placeholders, len(log.handlers))"
    )
    assert out == "1 1"


def _function_from_imports(path: Path):
    """(line, module, name) for every `from steady_py... import name` in path that binds a function.
    Imports inside `if TYPE_CHECKING:` are skipped: they never run, so they cannot hide a patch."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skipped = {id(node) for stmt in ast.walk(tree) if isinstance(stmt, ast.If)
               and isinstance(stmt.test, ast.Name) and stmt.test.id == "TYPE_CHECKING"
               for inner in stmt.body for node in ast.walk(inner)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or id(node) in skipped:
            continue
        if not (node.module or "").startswith("steady_py"):
            continue
        source = importlib.import_module(node.module)
        for alias in node.names:
            if inspect.isfunction(getattr(source, alias.name, None)):
                yield node.lineno, node.module, alias.name


def test_package_modules_never_from_import_a_function():
    """Tests replace functions by setting the attribute on their defining module. A caller that did
    `from steady_py.x import f` keeps the original, so the patch silently does nothing. Functions are
    therefore called through their module (`x.f(...)`); classes and constants may be from-imported.
    __init__.py is exempt: its re-exports are the public API, not internal callers."""
    package_dir = Path(steady_py.__file__).resolve().parent
    offenders = [
        f"{path.name}:{line}: from {module} import {name}"
        for path in sorted(package_dir.glob("*.py")) if path.name != "__init__.py"
        for line, module, name in _function_from_imports(path)
    ]
    assert offenders == []


def _unimported_submodule_uses(path: Path) -> list:
    """`pkg.sub` attribute uses where `pkg.sub` is a submodule the file never imports. They work only
    while some other module happens to import it first (K4: importlib.machinery)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    packages = {name.split(".")[0] for name in imported if "." in name}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in packages:
            dotted = f"{node.value.id}.{node.attr}"
            if dotted not in imported and importlib.util.find_spec(dotted) is not None:
                found.add(f"{path.name}:{node.lineno}: {dotted}")
    return sorted(found)


def test_every_submodule_used_is_imported_explicitly():
    package_dir = Path(steady_py.__file__).resolve().parent
    assert [u for path in sorted(package_dir.glob("*.py")) for u in _unimported_submodule_uses(path)] == []
