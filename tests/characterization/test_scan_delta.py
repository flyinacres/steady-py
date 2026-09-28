"""What scan reports against the manifest a notebook already carries: packages added, removed, and at a
new version. Delta for flags, raw installs and local modules is P3 (tests/behavior/test_writing.py)."""
from importlib.metadata import version

import pytest

from tests.support.manifests import altered, snapshotted
from tests.support.notebooks import Notebook, code
from tests.support.runner import run

PACKAGING, RESOLVELIB = version("packaging"), version("resolvelib")
EMPTY = {"added": [], "removed": [], "version_changes": []}

CHANGES = {  # (cells after the snapshot, manifest edit, the delta list that records it)
    "added": (["import resolvelib"], None,
              ("added", {"name": "resolvelib", "old_version": None, "new_version": RESOLVELIB})),
    "removed": ([], lambda m: m["dependencies"].append({"name": "resolvelib", "version": RESOLVELIB, "flags": []}),
                ("removed", {"name": "resolvelib", "old_version": RESOLVELIB, "new_version": None})),
    "version-changed": ([], lambda m: m["dependencies"][0].update(version="1.0"),
                        ("version_changes", {"name": "packaging", "old_version": "1.0", "new_version": PACKAGING})),
}


def _delta(path):
    outcome = run("scan", path)
    assert outcome.exit_code == 0, outcome.log
    return outcome.delta()


@pytest.fixture(autouse=True)
def _registered(pypi):
    pypi.add("packaging", {PACKAGING: {}})
    pypi.add("resolvelib", {RESOLVELIB: {}})


@pytest.mark.parametrize("change", CHANGES)
def test_delta_reports_each_package_change(tmp_path, change):
    extra, edit, (field, entry) = CHANGES[change]
    nb = snapshotted(tmp_path, code("import packaging"), extra=map(code, extra))
    if edit:
        altered(nb, edit)
    delta = _delta(nb)
    assert delta["has_changes"] is True
    assert {k: delta[k] for k in EMPTY} == {**EMPTY, field: [entry]}


def test_an_unchanged_notebook_has_an_empty_delta(tmp_path):
    delta = _delta(snapshotted(tmp_path, code("import packaging"), extra=[code("x = 1")]))
    assert delta["has_changes"] is False
    assert {k: delta[k] for k in EMPTY} == EMPTY


def test_a_notebook_without_a_manifest_has_no_delta(tmp_path):
    assert _delta(Notebook(code("import packaging")).write(tmp_path)) is None
