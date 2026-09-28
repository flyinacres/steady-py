"""Reading notebooks: IPython syntax anywhere in a notebook must not cost the tool its manifest
or a cell's imports."""
from importlib.metadata import version

import pytest

from tests.support.manifests import snapshotted
from tests.support.markers import known_bug
from tests.support.notebooks import Notebook, code, md
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


def _snapshot(tmp_path, pypi, *extra):
    """A notebook importing packaging, snapshotted, with `extra` cells added afterward."""
    pypi.add("packaging", {version("packaging"): {}})
    return snapshotted(tmp_path, code("import packaging"), extra=extra)


@pytest.mark.parametrize("source", _cases("K1", "one IPython line breaks manifest extraction"))
def test_check_reads_the_manifest(tmp_path, pypi, source):
    outcome = run("check", _snapshot(tmp_path, pypi, code(source)))
    assert outcome.exit_code == 0, outcome.log
    assert outcome.pins() == {"packaging": version("packaging")}


@pytest.mark.parametrize("source", _cases("K1", "one IPython line breaks manifest extraction"))
def test_scan_reports_the_delta_against_the_manifest(tmp_path, pypi, source):
    outcome = run("scan", _snapshot(tmp_path, pypi, code(source)))
    assert outcome.exit_code == 0, outcome.log
    assert outcome.delta() is not None, "manifest not read"
    assert outcome.delta()["has_changes"] is False


@pytest.mark.parametrize("source", _cases("K2", "an unparseable cell drops its imports silently"))
def test_imports_in_a_cell_with_ipython_syntax_are_found_or_reported(tmp_path, source):
    outcome = run("scan", Notebook(code(f"import packaging\n{source}")).write(tmp_path))
    assert outcome.exit_code in (0, 1)
    assert "packaging" in outcome.pins() or outcome.warnings()


@known_bug("G1", "a guarded install line erases every import in its cell")
def test_guarded_install_line_keeps_the_cells_imports(tmp_path):
    cell = code("import packaging\nIN_COLAB = False\nif IN_COLAB:\n    %pip install xyz")
    outcome = run("scan", Notebook(cell).write(tmp_path))
    assert outcome.exit_code in (0, 1), outcome.log
    assert "packaging" in outcome.pins()


@pytest.mark.parametrize("source", [
    pytest.param("%%writefile train.py\nimport torch", id="first-line"),
    pytest.param("\n\n%%writefile train.py\nimport torch", id="after-blank-lines",
                 marks=known_bug("G13", "%%writefile after blank lines is scanned as notebook code")),
])
def test_writefile_imports_are_not_notebook_dependencies(tmp_path, source):
    # IPython strips leading blank lines and runs the cell as run_cell_magic('writefile', ...).
    outcome = run("scan", Notebook(code(source)).write(tmp_path))
    assert outcome.exit_code in (0, 1), outcome.log
    assert outcome.dependency("torch")["status"] == "writefile_script"


@known_bug("P7", "cell numbers count code cells only, so they match no position the user sees")
def test_diagnostics_number_cells_by_notebook_position(tmp_path):
    cell = code("%pip install -r req.txt\n!conda install numpy")
    plain = run("scan", Notebook(cell).write(tmp_path / "plain"))
    after_markdown = run("scan", Notebook(md("# A"), md("# B"), md("# C"), cell).write(tmp_path / "md"))
    positions = [{d["cell_idx"] for d in (*o.warnings(), *o.notices())} for o in (plain, after_markdown)]
    assert len(positions[0]) == 1  # the pip warning and the conda notice name the same cell
    assert positions[1] == {positions[0].pop() + 3}


@known_bug("P7", "a directory report collapses identical notices and names no notebook")
def test_directory_report_names_each_notebook_with_a_notice(tmp_path):
    for name in ("alpha", "beta"):
        Notebook(code("!conda install numpy")).write(tmp_path, f"{name}.ipynb")
    outcome = run("scan", tmp_path, "--format", "text")
    assert outcome.exit_code in (0, 1), outcome.log
    # Text is the contract: the console report is where the creator reads which notebook to fix.
    assert "alpha.ipynb" in outcome.stdout and "beta.ipynb" in outcome.stdout


@known_bug("D1", "Cell 2's own import of steady_py is reported as a platform-provided dependency")
def test_scan_of_a_snapshotted_notebook_ignores_the_setup_cells_import(tmp_path, pypi):
    outcome = run("scan", _snapshot(tmp_path, pypi))
    assert outcome.exit_code == 0, outcome.log
    assert outcome.dependency("steady_py") is None
