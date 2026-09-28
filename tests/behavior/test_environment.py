"""Capturing the creator's installed environment."""
import pytest

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
