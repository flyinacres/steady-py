"""Where snapshot writes, what check says about an untouched manifest, and that any hand edit is caught."""
from importlib.metadata import version

import pytest

from tests.support.manifests import altered, snapshotted
from tests.support.notebooks import Notebook, code
from tests.support.outcomes import managed_cells, manifest
from tests.support.runner import run

PACKAGING = version("packaging")
SOURCE = code("import packaging")


@pytest.fixture
def plain(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    return Notebook(SOURCE).write(tmp_path / "src")


@pytest.mark.parametrize("flags, written", [
    (["--output"], "src/nb_merged.ipynb"),
    (["--output-dir", "out"], "out/nb.ipynb"),
])
def test_companion_modes_write_a_new_notebook_and_leave_the_source(plain, tmp_path, flags, written):
    before = plain.read_bytes()
    outcome = run("snapshot", plain, *[tmp_path / f if f == "out" else f for f in flags])
    assert outcome.exit_code == 0, outcome.log
    assert [p.relative_to(tmp_path).as_posix() for p in outcome.written] == [written]
    assert set(managed_cells(tmp_path / written)) == {"setup_markdown", "setup_code"}
    assert manifest(tmp_path / written)["dependencies"][0]["version"] == PACKAGING
    assert plain.read_bytes() == before


def test_in_place_rewrites_the_source(plain):
    outcome = run("snapshot", plain, "--in-place")
    assert outcome.exit_code == 0, outcome.log
    assert outcome.written == [plain]
    assert manifest(plain)["dependencies"][0]["version"] == PACKAGING


def test_without_a_write_flag_nothing_is_written(plain):
    before = sorted(plain.parent.iterdir())
    outcome = run("snapshot", plain)
    assert outcome.exit_code == 0, outcome.log
    assert outcome.written == []
    assert sorted(plain.parent.iterdir()) == before


@pytest.mark.parametrize("flags", [[], ["--output"], ["--output-dir", "out"]])
def test_an_existing_manifest_is_left_alone_without_in_place(tmp_path, pypi, flags):
    pypi.add("packaging", {PACKAGING: {}})
    nb = snapshotted(tmp_path, SOURCE)
    before = nb.read_bytes()
    assert run("snapshot", nb, *[tmp_path / f if f == "out" else f for f in flags]).exit_code == 0
    assert nb.read_bytes() == before


def test_in_place_replaces_an_existing_manifest(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    nb = altered(snapshotted(tmp_path, SOURCE), lambda m: m.update(tool_version="0.0.1"))
    assert run("snapshot", nb, "--in-place").exit_code == 0
    assert manifest(nb)["tool_version"] != "0.0.1"  # manifest() also requires exactly one


def test_check_of_a_fresh_snapshot_is_clean(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    nb = snapshotted(tmp_path, SOURCE)
    outcome = run("check", nb)
    assert outcome.exit_code == 0, outcome.log
    assert outcome.findings() == []
    assert outcome.report["manifest"]["dependency_hash"] == manifest(nb)["dependency_hash"]


HAND_EDITS = {
    "python_version": lambda m: m["python_version"].update(minor=m["python_version"]["minor"] - 1),
    "dependencies": lambda m: m["dependencies"][0].update(version="0.1"),
    "gpu": lambda m: m.update(gpu={"has_gpu": True}),
    "generated_at": lambda m: m.update(generated_at="2020-01-01 00:00:00"),
    "tool_version": lambda m: m.update(tool_version="0.0.1"),
    "schema_version": lambda m: m.update(schema_version="0.9"),
    "raw_installs": lambda m: m["raw_installs"].append({"spec": "extra", "flags": []}),
    "custom_sourced": lambda m: m["custom_sourced"].append("packaging"),
    "local_modules": lambda m: m["local_modules"].append({"name": "helper", "anchor": "notebook_dir"}),
    "baseline": lambda m: m.update(baseline=None),
    "custom_sourced-deleted": lambda m: m.pop("custom_sourced"),
}


@pytest.mark.parametrize("edit", HAND_EDITS.values(), ids=HAND_EDITS.keys())
def test_any_hand_edit_is_reported_as_tampered(tmp_path, pypi, edit):
    pypi.add("packaging", {PACKAGING: {}})
    nb = altered(snapshotted(tmp_path, SOURCE), edit, rehash=False)
    outcome = run("check", nb)
    assert outcome.exit_code == 1, outcome.log
    assert [f["severity"] for f in outcome.findings(signal="tampered")] == ["confirmed"]
