"""Capturing the creator's installed environment."""
import pytest

from tests.support.envs import uninstall
from tests.support.markers import known_bug
from tests.support.notebooks import Notebook, code
from tests.support.runner import run_in
from tests.support.sites import SiteDir, pythonpath


@pytest.mark.venv
@known_bug("E6", "a non-PEP 440 version is stored with the stray '=' of pip freeze's '==='")
def test_legacy_version_is_recorded_verbatim(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    site.add("legacypkg", "0.8.1ubuntu1")
    outcome = run_in(base_venv, "scan", Notebook(code("import legacypkg")).write(tmp_path), env=pythonpath(site))
    assert outcome.exit_code in (0, 1), outcome.log
    assert outcome.dependency("legacypkg")["version"] == "0.8.1ubuntu1"


@pytest.mark.venv
@known_bug("E1", "a failed pip freeze gives an empty 'verified' manifest and exit 0")
@pytest.mark.parametrize("verb", [["scan"], ["snapshot", "--output"]], ids=["scan", "snapshot"])
def test_failed_freeze_stops_the_run_and_writes_nothing(tmp_path, fresh_venv, verb):
    uninstall(fresh_venv, "pip")  # no pip module, as in a uv venv
    outcome = run_in(fresh_venv, verb[0], Notebook(code("import packaging")).write(tmp_path), *verb[1:])
    assert outcome.exit_code == 2, outcome.log
    assert list(tmp_path.glob("*_merged.ipynb")) == []
