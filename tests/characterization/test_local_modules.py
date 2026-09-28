"""Local modules are recorded with the directory they were found in, and check re-verifies them there:
present is clean, a missing module is drift, a missing or unsupplied directory can't be checked."""
import pytest

from tests.support.notebooks import Notebook, code
from tests.support.outcomes import manifest
from tests.support.runner import run


def _check(*argv, exit_code):
    outcome = run("check", *argv)
    assert outcome.exit_code == exit_code, outcome.log
    return [(f["signal"], f["severity"], f["package"]) for f in outcome.findings()]


@pytest.fixture
def beside(tmp_path):
    """A notebook snapshotted in place, importing a sibling module and a sibling package."""
    nb = Notebook(code("import helper\nimport helperpkg.sub")).write(tmp_path)
    (tmp_path / "helper.py").touch()
    (tmp_path / "helperpkg").mkdir()
    (tmp_path / "helperpkg" / "__init__.py").touch()
    assert run("snapshot", nb, "--in-place").exit_code == 0
    return nb


@pytest.fixture
def at_root(tmp_path):
    """A notebook in proj/, snapshotted as part of its directory, importing a module at the root."""
    nb = Notebook(code("import roothelper")).write(tmp_path / "proj")
    (tmp_path / "roothelper.py").touch()
    assert run("snapshot", tmp_path, "--in-place").exit_code == 0
    return nb


def test_a_sibling_module_is_anchored_to_the_notebook_directory(beside):
    assert manifest(beside)["local_modules"] == [{"name": "helper", "anchor": "notebook_dir"},
                                                 {"name": "helperpkg", "anchor": "notebook_dir"}]


def test_a_module_at_the_run_root_is_anchored_to_the_root(at_root):
    assert manifest(at_root)["local_modules"] == [{"name": "roothelper", "anchor": "root_dir"}]


def test_check_reverifies_a_sibling_module(beside, tmp_path):
    assert _check(beside, exit_code=0) == []
    (tmp_path / "helper.py").unlink()
    assert _check(beside, exit_code=1) == [("local_module_missing", "confirmed", "helper")]


@pytest.mark.parametrize("root", ["flag", "directory-run"])
def test_check_reverifies_a_root_module_against_the_root(at_root, tmp_path, root):
    argv = [at_root, "--root-dir", tmp_path] if root == "flag" else [tmp_path]
    assert run("check", *argv).exit_code == 0
    (tmp_path / "roothelper.py").unlink()
    assert run("check", *argv).exit_code == 1


@pytest.mark.parametrize("root_dir", [None, "moved"], ids=["not-supplied", "directory-gone"])
def test_a_root_module_that_cant_be_located_is_unverifiable(at_root, tmp_path, root_dir):
    argv = [at_root] + (["--root-dir", tmp_path / root_dir] if root_dir else [])
    assert _check(*argv, exit_code=2) == [("local_module_unverifiable", "error", "roothelper")]
