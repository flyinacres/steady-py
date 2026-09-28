"""Tests under tests/behavior and tests/characterization reach steady-py only through its public API and tests/support, so
they survive the rearchitecture. This check keeps them honest."""
import ast
from pathlib import Path

import steady_py

BOUNDARY_DIRS = [Path(__file__).resolve().parents[1] / d for d in ("behavior", "characterization")]
PUBLIC = set(steady_py.__all__)


def violations(source: str) -> list:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.startswith("steady_py.")]
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "steady_py":
            names = [a.name for a in node.names]
            if node.module != "steady_py" or not PUBLIC.issuperset(names):
                found.append(f"from {node.module} import {', '.join(names)}")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "steady_py" and node.attr not in PUBLIC:
            found.append(f"steady_py.{node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("steady_py."):
            found.append(repr(node.value))  # patch targets, import_module strings
    return found


def test_boundary_tests_use_only_the_public_api():
    bad = {p.name: v for d in BOUNDARY_DIRS for p in d.rglob("*.py") if (v := violations(p.read_text(encoding="utf-8")))}
    assert bad == {}


def test_checker_catches_each_route_to_internals():
    for source in ("import steady_py.resolution", "from steady_py.drift import x",
                   "from steady_py import resolution", "import steady_py\nsteady_py.models.X",
                   "mock.patch('steady_py.pypi.fetch')", "from steady_py import *"):
        assert violations(source), source
    assert violations("import steady_py\nfrom steady_py import scan\nsteady_py.check(1)") == []
