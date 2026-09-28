"""An import is pinned under the name of the distribution that provides it: from the environment's
metadata (top_level.txt, or RECORD alone), or, where the metadata lists no modules (a conda stub),
from steady-py's alias table for well-known names."""
import pytest

from tests.support.notebooks import Notebook, code
from tests.support.outcomes import manifest
from tests.support.runner import run_in
from tests.support.sites import CONDA_STUB, SiteDir, pythonpath

CASES = {  # id: (distribution, SiteDir.add shape, import)
    "metadata-top-level": ("Acme-Toolkit", {"modules": ["acmetk/__init__.py"]}, "acmetk"),
    "metadata-record-only": ("Acme-Toolkit", {"modules": ["acmetk/__init__.py"], "top_level": False}, "acmetk"),
    "alias-table": ("PyYAML", CONDA_STUB, "yaml"),
}


@pytest.mark.venv
@pytest.mark.parametrize("case", CASES)
def test_import_is_pinned_as_its_distribution(tmp_path, base_venv, pypi, case):
    dist, shape, module = CASES[case]
    site = SiteDir(tmp_path / "site")
    site.add(dist, "1.2.0", **shape)
    pypi.add(dist, {"1.2.0": {}})  # strict: a lookup under the import name fails the test
    nb = Notebook(code(f"import {module}")).write(tmp_path / "nb")
    outcome = run_in(base_venv, "snapshot", nb, "--output", env=pythonpath(site))
    assert outcome.exit_code == 0, outcome.log
    assert manifest(outcome.written[0])["dependencies"] == [{"name": dist, "version": "1.2.0", "flags": []}]
