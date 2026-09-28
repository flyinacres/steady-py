"""What snapshot and scan write and report: which notebooks, which cells, which changes."""
import json
from importlib.metadata import version

import pytest

from tests.support.manifests import snapshotted
from tests.support.markers import finding, known_bug
from tests.support.notebooks import Notebook, code, write_json
from tests.support.outcomes import managed_cells
from tests.support.runner import run

PACKAGING = version("packaging")
ENVIRONMENTS = ("myenv", "condaenv", "lib/site-packages")  # a venv, a conda env, installed packages


@finding("DR1")
def test_directory_runs_skip_environments_whatever_their_name(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    Notebook(code("import packaging")).write(tmp_path, "proj.ipynb")
    (tmp_path / "myenv").mkdir()
    (tmp_path / "myenv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (tmp_path / "condaenv" / "conda-meta").mkdir(parents=True)
    for env in ENVIRONMENTS:  # wheels ship notebooks; these must never be read or rewritten
        Notebook(code("import packaging")).write(tmp_path / env / "pkg", "shipped.ipynb")
    scan = run("scan", tmp_path)
    assert scan.exit_code in (0, 1), scan.log
    assert [p.relative_to(tmp_path).as_posix() for p in scan.notebooks()] == ["proj.ipynb"]
    snapshot = run("snapshot", tmp_path, "--output")
    assert snapshot.exit_code == 0, snapshot.log
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*_merged.ipynb")) == ["proj_merged.ipynb"]


@pytest.mark.parametrize("prior, warns", [
    pytest.param("scratch", True, marks=known_bug("K7", "a user cell that mentions the manifest is dropped silently")),
    pytest.param("setup-cell", False, id="untagged-setup-cell"),
])
def test_replacing_a_cell_with_other_code_warns(tmp_path, pypi, prior, warns):
    pypi.add("packaging", {PACKAGING: {}})
    if prior == "scratch":
        nb = Notebook(code("import packaging"),
                      code("def f():\n    STEADY_PY_MANIFEST = {}\n    return 1\nx = 2")).write(tmp_path)
    else:  # a setup cell pasted from elsewhere, so without its managed tags
        data = json.loads(snapshotted(tmp_path, code("import packaging")).read_text(encoding="utf-8"))
        for cell in data["cells"]:
            cell["metadata"] = {}
        nb = write_json(data, tmp_path, "pasted.ipynb")
    outcome = run("snapshot", nb, "--in-place")
    assert outcome.exit_code == 0, outcome.log
    assert bool(outcome.warnings()) is warns


@pytest.mark.parametrize("cells, changed", [
    pytest.param(["%pip install packaging --index-url https://idx.test/simple"], True, id="flags",
                 marks=known_bug("P3", "delta ignores a pin's flags")),
    pytest.param(["import helper"], True, id="local-module",
                 marks=known_bug("P3", "delta ignores local modules")),
    pytest.param(["x = 1"], False, id="no-change"),
])
def test_delta_covers_every_manifest_field(tmp_path, pypi, cells, changed):
    pypi.add("packaging", {PACKAGING: {}})
    nb = snapshotted(tmp_path, code("import packaging"), extra=map(code, cells))
    (nb.parent / "helper.py").write_text("")
    outcome = run("scan", nb)
    assert outcome.exit_code in (0, 1), outcome.log
    assert outcome.delta()["has_changes"] is changed


@known_bug("D4", "a single-file scan has no text report")
def test_single_file_scan_has_a_text_report(tmp_path):
    outcome = run("scan", Notebook(code("import packaging")).write(tmp_path), "--format", "text")
    assert outcome.exit_code in (0, 1), outcome.log
    assert "packaging" in outcome.stdout


@known_bug("P5", "managed cells get new IDs on every snapshot")
def test_resnapshot_keeps_managed_cell_ids(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    nb = Notebook(code("import packaging")).write(tmp_path)
    ids = []
    for _ in range(2):
        assert run("snapshot", nb, "--in-place").exit_code == 0
        ids.append({role: cell["id"] for role, cell in managed_cells(nb).items()})
    assert ids[0] == ids[1]
