"""What snapshot writes and reports: which notebooks, which cells, which changes."""
import json
from importlib.metadata import version

import pytest

from tests.support.manifests import altered, snapshotted
from tests.support.markers import finding, known_bug
from tests.support.notebooks import Notebook, code, write_json
from tests.support.outcomes import managed_cells, manifest
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
    scan = run("snapshot", tmp_path)
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
    # The scratch cell also reads as an unreadable manifest, a warning that doesn't say the cell is dropped.
    assert bool([w for w in outcome.warnings() if w["type"] != "unreadable_manifest"]) is warns


@finding("P3")
@pytest.mark.parametrize("cells, changed", [
    pytest.param(["%pip install packaging --index-url https://idx.test/simple"], True, id="flags"),
    pytest.param(["import helper"], True, id="local-module"),
    pytest.param(["x = 1"], False, id="no-change"),
])
def test_delta_covers_every_manifest_field(tmp_path, pypi, cells, changed):
    pypi.add("packaging", {PACKAGING: {}})
    nb = snapshotted(tmp_path, code("import packaging"), extra=map(code, cells))
    (nb.parent / "helper.py").write_text("")
    outcome = run("snapshot", nb)
    assert outcome.exit_code in (0, 1), outcome.log
    assert outcome.delta()["has_changes"] is changed


@finding("D4")
def test_the_first_command_a_user_tries_reports_in_text(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    outcome = run("snapshot", Notebook(code("import packaging")).write(tmp_path), "--format", "text")
    assert outcome.exit_code in (0, 1), outcome.log
    assert "packaging" in outcome.stdout
    assert run("scan", tmp_path).exit_code == 2  # the scan verb is gone (argparse rejects it)


@finding("P5")
def test_an_unchanged_notebook_is_not_rewritten(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    nb = Notebook(code("import packaging")).write(tmp_path)
    assert run("snapshot", nb, "--in-place").exit_code == 0
    before = nb.read_bytes()
    outcome = run("snapshot", nb, "--in-place")
    assert outcome.exit_code == 0, outcome.log
    assert outcome.delta()["has_changes"] is False
    assert nb.read_bytes() == before and outcome.written == []


@finding("P5")
def test_a_change_keeps_the_dates_and_cell_ids_of_what_did_not_change(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    pypi.add("resolvelib", {version("resolvelib"): {}})
    nb = Notebook(code("import packaging")).write(tmp_path)
    assert run("snapshot", nb, "--in-place").exit_code == 0
    first, ids = manifest(nb), {role: cell["id"] for role, cell in managed_cells(nb).items()}
    data = json.loads(nb.read_text(encoding="utf-8"))
    data["cells"].append(Notebook(code("import resolvelib")).to_json()["cells"][0])
    write_json(data, tmp_path, nb.name)
    _age(nb, first)
    assert run("snapshot", nb, "--in-place").exit_code == 0
    second = manifest(nb)
    dates = {p["name"]: p["changed_at"] for p in second["packages"]}
    assert dates["packaging"] == "2001-01-01 00:00:00" and dates["resolvelib"] != dates["packaging"]
    assert second["generated_at"] == dates["resolvelib"]
    assert {role: cell["id"] for role, cell in managed_cells(nb).items()} == ids


def _age(path, literal):
    """Backdates every date in the manifest at `path`, through the one place tests change a manifest."""
    def backdate(m):
        m["generated_at"] = "2001-01-01 00:00:00"
        for entry in m["packages"] + m["local_modules"]:
            entry["changed_at"] = "2001-01-01 00:00:00"
    altered(path, backdate)


@finding("CH2")
def test_a_name_spelled_differently_is_the_same_package(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    nb = altered(snapshotted(tmp_path, code("import packaging")),
                 lambda m: m["packages"][0].update(name="Packaging", display_name="Packaging"))
    outcome = run("snapshot", nb)
    assert outcome.exit_code == 0, outcome.log
    assert outcome.delta()["has_changes"] is False


@finding("DG4")
def test_packages_cell2_does_not_install_are_recorded_with_their_status(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    source = "try:\n    import resolvelib\nexcept ImportError:\n    pass\nimport notinstalled_zz\nimport packaging"
    outcome = run("snapshot", Notebook(code(source)).write(tmp_path), "--output")
    assert outcome.exit_code in (0, 1), outcome.log
    statuses = {p["name"]: p["status"] for p in manifest(outcome.written[0])["packages"]}
    assert statuses == {"resolvelib": "guarded", "notinstalled-zz": "not_installed", "packaging": "pinned"}


# The previous manifest is read in every mode (manifest regeneration spec, item 2).

@pytest.mark.parametrize("flags", [("--output",), ("--output-dir", "out")], ids=["companion", "directory"])
def test_a_write_compares_with_the_file_it_replaces(tmp_path, pypi, flags):
    pypi.add("packaging", {PACKAGING: {}})
    pypi.add("resolvelib", {version("resolvelib"): {}})
    flags = tuple(str(tmp_path / f) if f == "out" else f for f in flags)
    nb = Notebook(code("import packaging")).write(tmp_path)
    assert run("snapshot", nb, *flags).exit_code == 0
    Notebook(code("import packaging"), code("import resolvelib")).write(tmp_path)
    outcome = run("snapshot", nb, *flags)
    assert outcome.exit_code == 0, outcome.log
    assert [p["name"] for p in outcome.delta()["added"]] == ["resolvelib"]


def test_a_write_with_nothing_to_replace_compares_with_the_source(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    outcome = run("snapshot", snapshotted(tmp_path, code("import packaging")), "--output")
    assert outcome.exit_code == 0, outcome.log
    assert outcome.delta()["has_changes"] is False


def test_an_unchanged_companion_keeps_its_date(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    nb = Notebook(code("import packaging")).write(tmp_path)
    companion = run("snapshot", nb, "--output").written[0]
    _age(companion, manifest(companion))
    assert run("snapshot", nb, "--output").exit_code == 0
    assert manifest(companion)["generated_at"] == "2001-01-01 00:00:00"
