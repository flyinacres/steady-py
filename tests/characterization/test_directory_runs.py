"""How much of a target each verb could process decides its exit code: 0 all of it, 1 some of it
(named loudly), 2 none of it. A directory run also folds findings shared across notebooks."""
from importlib.metadata import version

import pytest

from tests.support.notebooks import Notebook, code
from tests.support.runner import run

PACKAGING = version("packaging")
VERBS = ("scan", "snapshot", "check")


def _unreadable(directory):
    path = directory / "bad.ipynb"
    path.write_text("{not json", encoding="utf-8")
    return path


TARGETS = {  # name: (builds the target in a directory, expected exit code)
    "missing-file": (lambda d: d / "absent.ipynb", 2),
    "missing-directory": (lambda d: d / "absent", 2),
    "unparseable-file": (_unreadable, 2),
    "directory-of-unreadable": (lambda d: _unreadable(d).parent, 2),
    "empty-directory": (lambda d: d, 0),
}


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("target", TARGETS)
def test_exit_code_by_how_much_could_be_processed(tmp_path, verb, target):
    build, exit_code = TARGETS[target]
    outcome = run(verb, build(tmp_path))
    assert outcome.exit_code == exit_code, outcome.log


@pytest.fixture
def partial(tmp_path, pypi):
    """A directory with one good notebook and one that can't be read."""
    pypi.add("packaging", {PACKAGING: {}})
    good = Notebook(code("import packaging")).write(tmp_path, "good.ipynb")
    return good, _unreadable(tmp_path)


def test_partial_snapshot_writes_the_rest_and_names_the_failure(partial, tmp_path):
    good, bad = partial
    outcome = run("snapshot", tmp_path, "--output")
    assert outcome.exit_code == 1, outcome.log
    assert outcome.written == [good.with_name("good_merged.ipynb")]
    assert outcome.unreadable() == [bad]
    assert bad.name in outcome.log


def test_partial_scan_reports_the_rest_and_names_the_failure(partial, tmp_path):
    good, bad = partial
    outcome = run("scan", tmp_path)
    assert outcome.exit_code == 1, outcome.log
    assert outcome.notebooks() == [good]
    assert outcome.unreadable() == [bad]


def test_partial_check_checks_the_rest_and_names_the_failure(partial, tmp_path):
    good, bad = partial
    assert run("snapshot", good, "--in-place").exit_code == 0
    outcome = run("check", tmp_path)
    assert outcome.exit_code == 1, outcome.log
    assert outcome.unreadable() == [bad]
    assert [g["package"] for g in outcome.validation()["findings"]] == []  # the good one was checked, clean
    assert outcome.validation()["notebooks_checked"] == 1


@pytest.mark.parametrize("verb", ["snapshot", "check"])
def test_a_finding_shared_by_notebooks_is_reported_once(tmp_path, pypi, verb):
    pypi.add("packaging", {PACKAGING: {}})
    for directory, name in ((tmp_path, "a.ipynb"), (tmp_path / "sub", "b.ipynb")):
        Notebook(code("import packaging")).write(directory, name)
    if verb == "check":
        assert run("snapshot", tmp_path, "--in-place").exit_code == 0
    pypi.add("packaging", {PACKAGING: {"yanked": True}})
    outcome = run(verb, tmp_path, *(["--output"] if verb == "snapshot" else []))  # validation runs on write
    assert outcome.exit_code == (1 if verb == "check" else 0), outcome.log
    validation = outcome.validation()
    assert [(g["signal"], g["notebooks"]) for g in validation["findings"]] == [("yanked", ["a.ipynb", "sub/b.ipynb"])]
    assert validation["totals"]["confirmed"] == 1
    assert [(n["path"], n["confirmed"]) for n in validation["notebooks"]] == [("a.ipynb", 1), ("sub/b.ipynb", 1)]
