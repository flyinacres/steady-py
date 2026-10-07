"""Reading notebooks: IPython syntax anywhere in a notebook must not cost the tool its manifest
or a cell's imports."""
import sys
from importlib.metadata import version

import pytest

from tests.support.manifests import snapshotted
from tests.support.markers import finding
from tests.support.notebooks import PYTHON_METADATA, Notebook, code, md
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


def _cases(finding_id: str) -> list:
    return [pytest.param("x = 1", id="plain-python")] + [
        pytest.param(src, id=name, marks=finding(finding_id)) for name, src in IPYTHON_CELLS.items()]


def _snapshot(tmp_path, pypi, *extra):
    """A notebook importing packaging, snapshotted, with `extra` cells added afterward."""
    pypi.add("packaging", {version("packaging"): {}})
    return snapshotted(tmp_path, code("import packaging"), extra=extra)


@pytest.mark.parametrize("source", _cases("K1"))
def test_check_reads_the_manifest(tmp_path, pypi, source):
    outcome = run("check", _snapshot(tmp_path, pypi, code(source)))
    assert outcome.exit_code == 0, outcome.log
    assert outcome.pins() == {"packaging": version("packaging")}


@pytest.mark.parametrize("source", _cases("K1"))
def test_scan_reports_the_delta_against_the_manifest(tmp_path, pypi, source):
    outcome = run("scan", _snapshot(tmp_path, pypi, code(source)))
    assert outcome.exit_code == 0, outcome.log
    assert outcome.delta() is not None, "manifest not read"
    assert outcome.delta()["has_changes"] is False


@pytest.mark.parametrize("source", _cases("K2"))
def test_imports_in_a_cell_with_ipython_syntax_are_found_or_reported(tmp_path, source):
    outcome = run("scan", Notebook(code(f"import packaging\n{source}")).write(tmp_path))
    assert outcome.exit_code in (0, 1)
    assert "packaging" in outcome.pins() or outcome.warnings()


@finding("G1")
def test_guarded_install_line_keeps_the_cells_imports(tmp_path):
    cell = code("import packaging\nIN_COLAB = False\nif IN_COLAB:\n    %pip install xyz")
    outcome = run("scan", Notebook(cell).write(tmp_path))
    assert outcome.exit_code in (0, 1), outcome.log
    assert "packaging" in outcome.pins()


@pytest.mark.parametrize("source", [
    pytest.param("%%writefile train.py\nimport torch", id="first-line"),
    pytest.param("\n\n%%writefile train.py\nimport torch", id="after-blank-lines",
                 marks=finding("G13")),
])
def test_writefile_imports_are_not_notebook_dependencies(tmp_path, source):
    # IPython strips leading blank lines and runs the cell as run_cell_magic('writefile', ...).
    outcome = run("scan", Notebook(code(source)).write(tmp_path))
    assert outcome.exit_code in (0, 1), outcome.log
    assert outcome.dependency("torch")["status"] == "writefile_script"


@finding("P7")
def test_diagnostics_number_cells_by_notebook_position(tmp_path):
    cell = code("%pip install -r req.txt\n!conda install numpy")
    plain = run("scan", Notebook(cell).write(tmp_path / "plain"))
    after_markdown = run("scan", Notebook(md("# A"), md("# B"), md("# C"), cell).write(tmp_path / "md"))
    positions = [{d["cell_idx"] for d in (*o.warnings(), *o.notices())} for o in (plain, after_markdown)]
    assert len(positions[0]) == 1  # the pip warning and the conda notice name the same cell
    assert positions[1] == {positions[0].pop() + 3}


@finding("P7")
def test_directory_report_names_each_notebook_with_a_notice(tmp_path):
    for name in ("alpha", "beta"):
        Notebook(code("!conda install numpy")).write(tmp_path, f"{name}.ipynb")
    outcome = run("scan", tmp_path, "--format", "text")
    assert outcome.exit_code in (0, 1), outcome.log
    # Text is the contract: the console report is where the creator reads which notebook to fix.
    assert "alpha.ipynb" in outcome.stdout and "beta.ipynb" in outcome.stdout


@finding("P7")
def test_install_lines_are_located_where_the_user_sees_them(tmp_path):
    notebook = Notebook(md("# Setup"), md("Installs follow."), code("x = 1\n%pip install packaging"))
    line, = run("scan", notebook.write(tmp_path)).install_lines()
    assert (line["text"], line["tool"], line["cell_idx"], line["line_idx"], line["heading"]) == (
        "%pip install packaging", "pip", 2, 1, "Setup")


@finding("D1")
def test_scan_of_a_snapshotted_notebook_ignores_the_setup_cells_import(tmp_path, pypi):
    outcome = run("scan", _snapshot(tmp_path, pypi))
    assert outcome.exit_code == 0, outcome.log
    assert outcome.dependency("steady_py") is None


# Parses, but nests deeper than the import visitor can walk at a recursion limit of 1,000 or 3,000
# (importing IPython can raise the limit to 3,000).
DEEP = "x = 1" + " + 1" * 3000


@finding("DR2")
@pytest.mark.parametrize("verb", ("scan", "snapshot"))
def test_one_notebooks_internal_error_does_not_end_a_directory_run(tmp_path, pypi, verb):
    """Exit 1, not a crash, shows the good notebook was processed; the failure is named."""
    pypi.add("packaging", {version("packaging"): {}})
    Notebook(code("import packaging")).write(tmp_path, "good.ipynb")
    # language_info must be a mapping; a string is an internal error today, and the only trigger
    # that needs no monkeypatching. Replace it when malformed metadata becomes a reported error.
    bad = Notebook(code("import json"), metadata={"language_info": "python"}).write(tmp_path, "bad.ipynb")
    outcome = run(verb, tmp_path)
    assert outcome.exit_code == 1, outcome.log
    assert outcome.unreadable() == [bad]
    assert f"Could not scan {bad}" in outcome.log


@finding("K13")
@pytest.mark.parametrize("target", ("file", "directory"))
def test_deeply_nested_expression_keeps_the_notebooks_imports(tmp_path, target):
    path = Notebook(code("import packaging"), code(DEEP)).write(tmp_path, "deep.ipynb")
    outcome = run("scan", path if target == "file" else tmp_path)
    assert outcome.exit_code in (0, 1), outcome.log
    if target == "directory":
        assert outcome.unreadable() == []
        names = [d["name"] for n in outcome.report["notebooks"] for d in n["dependencies"]]
        too_deep = [w for n in outcome.report["notebooks"] for w in n["warnings"] if w["type"] == "cell_too_deep"]
    else:
        names = [d["name"] for d in outcome.dependencies()]
        too_deep = outcome.warnings("cell_too_deep")
    assert "packaging" in names
    assert [w["cell_idx"] for w in too_deep] == [1]


@finding("K2")
def test_unparseable_cell_is_named_with_both_python_versions(tmp_path):
    # `lazy import` is Python 3.15 syntax (PEP 810).
    metadata = {**PYTHON_METADATA, "language_info": {"name": "python", "version": "3.15.0"}}
    outcome = run("scan", Notebook(code("import packaging"), code("lazy import json"), metadata=metadata).write(tmp_path))
    assert outcome.exit_code in (0, 1), outcome.log
    warning, = outcome.warnings("unparseable_cell")
    assert warning["cell_idx"] == 1
    assert "3.15.0" in warning["detail"] and f"{sys.version_info.major}.{sys.version_info.minor}" in warning["detail"]
    assert "packaging" in outcome.pins()


@pytest.mark.parametrize("source, found", [
    pytest.param("%%capture\nimport packaging", True, id="python-body"),
    pytest.param("\n%%time\n%%capture\nimport packaging", True, id="nested-after-blank-line"),
    pytest.param("%%html\n<p>import packaging</p>", False, id="not-python"),
])
@finding("K2")
def test_cell_magic_bodies_are_read_as_python_only_where_ipython_runs_them_so(tmp_path, source, found):
    outcome = run("scan", Notebook(code(source)).write(tmp_path))
    assert outcome.exit_code in (0, 1), outcome.log
    assert ("packaging" in outcome.pins()) is found
    assert outcome.warnings("unparseable_cell") == []
