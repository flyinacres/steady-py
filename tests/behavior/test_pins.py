"""Which pin, and which status, each dependency gets."""
import pytest

from tests.support.markers import known_bug
from tests.support.notebooks import Notebook, code
from tests.support.runner import run


@pytest.mark.parametrize("module, installed", [
    ("packaging", True),
    pytest.param("steady_py_absent_mod", False, marks=known_bug("P6", "not-found imports are labeled pinned")),
])
def test_status_pinned_only_for_installed_imports(tmp_path, module, installed):
    outcome = run("scan", Notebook(code(f"import {module}")).write(tmp_path))
    assert outcome.exit_code in (0, 1)
    assert (outcome.dependency(module)["status"] == "pinned") is installed
