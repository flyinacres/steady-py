"""Reading notebooks: IPython syntax anywhere in a notebook must not cost the tool its manifest
or a cell's imports."""
from importlib.metadata import version

import pytest

from tests.support.markers import known_bug
from tests.support.notebooks import Notebook, code
from tests.support.runner import run

# Valid in IPython. The first three fail a plain ast.parse; the last three parse, but not after
# the tool blanks every line starting with % or !.
IPYTHON_CELLS = {
    "assign-shell": "files = !ls",
    "assign-magic": "t = %timeit -n1 -r1 pass",
    "help": "df = 1\ndf?",
    "string-closing-on-percent": 's = """\n%d items"""',
    "continuation-starting-percent": "x = 10 \\\n% 3",
    "condition-starting-not-equal": "a = b = 1\nok = (a\n!= b)",
}


def _cases(finding_id: str, why: str) -> list:
    return [pytest.param("x = 1", id="plain-python")] + [
        pytest.param(src, id=name, marks=known_bug(finding_id, why)) for name, src in IPYTHON_CELLS.items()]


def _snapshot(tmp_path, pypi, *cells) -> str:
    """A notebook with a manifest, from a real snapshot. Raises RuntimeError, not AssertionError,
    so a failed setup can't pass as an expected failure."""
    pypi.add("packaging", {version("packaging"): {}})
    outcome = run("snapshot", Notebook(*cells).write(tmp_path), "--output")
    if outcome.exit_code != 0:
        raise RuntimeError(f"snapshot failed (exit {outcome.exit_code}):\n{outcome.log}")
    return outcome.written[0]


@pytest.mark.parametrize("source", _cases("K1", "one IPython line breaks manifest extraction"))
def test_check_reads_the_manifest(tmp_path, pypi, source):
    outcome = run("check", _snapshot(tmp_path, pypi, code("import packaging"), code(source)))
    assert outcome.exit_code == 0, outcome.log
    assert outcome.pins() == {"packaging": version("packaging")}


@pytest.mark.parametrize("source", _cases("K1", "one IPython line breaks manifest extraction"))
def test_scan_reports_the_delta_against_the_manifest(tmp_path, pypi, source):
    outcome = run("scan", _snapshot(tmp_path, pypi, code("import packaging"), code(source)))
    assert outcome.exit_code == 0, outcome.log
    assert outcome.delta() is not None, "manifest not read"
    assert outcome.delta()["has_changes"] is False


@pytest.mark.parametrize("source", _cases("K2", "an unparseable cell drops its imports silently"))
def test_imports_in_a_cell_with_ipython_syntax_are_found_or_reported(tmp_path, source):
    outcome = run("scan", Notebook(code(f"import packaging\n{source}")).write(tmp_path))
    assert outcome.exit_code in (0, 1)
    assert "packaging" in outcome.pins() or outcome.warnings()
