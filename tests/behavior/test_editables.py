"""The creator's own in-development packages: editable installs and local modules."""
import pytest

from tests.support.envs import install_project
from tests.support.markers import known_bug
from tests.support.notebooks import Notebook, code
from tests.support.runner import run_in


@pytest.mark.venv
@known_bug("ED3", "an editable import is only a Cell 2 comment: no warning, exit 0")
def test_editable_import_is_reported_as_needing_attention(tmp_path, fresh_venv):
    install_project(fresh_venv, "demo-editable", tmp_path / "projects", editable=True)
    outcome = run_in(fresh_venv, "scan", Notebook(code("import demo_editable")).write(tmp_path / "nb"))
    assert outcome.exit_code == 1, outcome.log
    assert outcome.warnings()
