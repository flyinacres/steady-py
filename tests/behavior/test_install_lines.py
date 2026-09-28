"""How install lines in the creator's notebook are harvested."""
import pytest

from tests.support.markers import known_bug
from tests.support.notebooks import Notebook, code
from tests.support.runner import run


@known_bug("G4", "a variable install line is harvested as a package named after the variable")
@pytest.mark.parametrize("line", ["%pip install $pkg", "!pip install {pkg}", "%pip install packaging $extra"])
def test_variable_install_line_is_a_warning_not_a_package(tmp_path, line):
    # IPython expands $name and {expr} at run time; a static scan can't know the value.
    outcome = run("scan", Notebook(code("pkg = extra = 'x'"), code(line)).write(tmp_path))
    assert outcome.exit_code in (0, 1)
    assert [d["name"] for d in outcome.dependencies() if set(d["name"]) & set("${}")] == []
    assert outcome.warnings()
