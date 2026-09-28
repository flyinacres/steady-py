"""F5's run_in runs the tool on the venv's own interpreter, sees site dirs in order, and nothing
from the test process's environment."""
import pytest

from tests.support.notebooks import Notebook, code
from tests.support.runner import run, run_in
from tests.support.sites import SiteDir, pythonpath


@pytest.mark.venv
def test_run_in_sees_only_the_venv_and_the_given_sites(tmp_path, base_venv, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "leak"))
    SiteDir(tmp_path / "leak").add("demo", "9.9")
    first, second = SiteDir(tmp_path / "first"), SiteDir(tmp_path / "second")
    first.add("demo", "1.0")
    second.add("demo", "0.9")
    path = Notebook(code("import demo")).write(tmp_path)
    outcome = run_in(base_venv, "scan", path, env=pythonpath(first, second))
    assert outcome.exit_code == 0, outcome.log
    assert outcome.report["environment"]["active_interpreter"].startswith(str(base_venv.path))
    assert outcome.pins()["demo"] == "1.0"
    assert run_in(base_venv, "scan", path).dependency("demo")["version"] is None  # no leak without sites
