"""What check (and snapshot's baseline) reports about pins against PyPI."""
from importlib.metadata import version

import pytest

from tests.support.manifests import altered, snapshotted
from tests.support.markers import finding, known_bug
from tests.support.notebooks import Notebook, code
from tests.support.outcomes import manifest
from tests.support.runner import run

PACKAGING, RESOLVELIB = version("packaging"), version("resolvelib")


def _check(path):
    outcome = run("check", path)
    assert outcome.report is not None, outcome.log
    return outcome


def _confirmed(outcome, **match):
    return [f for f in outcome.findings(**match) if f["severity"] == "confirmed"]


@finding("K3")
@pytest.mark.parametrize("mode", ["drop", "truncate", "500"])  # 500: the path that already worked
def test_dropped_connection_is_a_network_error(tmp_path, pypi, mode):
    pypi.add("packaging", {PACKAGING: {}})
    nb = snapshotted(tmp_path, code("import packaging"))
    pypi.fail("packaging", mode)
    outcome = _check(nb)
    assert outcome.exit_code == 2  # could not do the job
    assert outcome.findings(signal="check_error", package="packaging")
    assert run("snapshot", Notebook(code("import packaging")).write(tmp_path / "again"), "--output").exit_code == 0


@pytest.mark.parametrize("unreachable", [
    pytest.param(True, marks=known_bug("K8", "a failed lookup of a transitive dependency is a confirmed conflict")),
    False,
])
def test_failed_lookup_is_not_a_confirmed_conflict(tmp_path, pypi, unreachable):
    pypi.add("resolvelib", {RESOLVELIB: {"requires_dist": ["demo-t>=1"]}})
    pypi.add("demo-t", {"0.5": {}})  # a real conflict when reachable
    nb = snapshotted(tmp_path, code("import resolvelib"))
    if unreachable:
        pypi.fail("demo-t", "500")
    outcome = _check(nb)
    assert outcome.findings(package="demo-t")
    assert bool(_confirmed(outcome, package="demo-t")) is not unreachable


# 2.0+cu121 is the public release 2.0 plus a build tag: PyPI's checks apply to 2.0.
LOCAL_PIN = f"{PACKAGING}+cu121"
OLD_BASELINE = {"version": 1, "findings": [["unverifiable_custom_index", "packaging", LOCAL_PIN]], "errors": []}


@known_bug("LV1", "a local-version pin skips every PyPI check")
@pytest.mark.parametrize("old_baseline", [False, True], ids=["current", "old-baseline"])
def test_local_version_pin_is_checked_as_its_public_release(tmp_path, pypi, old_baseline):
    pypi.add("packaging", {"0.9": {}, PACKAGING: {"yanked": True}})
    pypi.add("resolvelib", {RESOLVELIB: {"requires_dist": [f"packaging<{PACKAGING}"]}})
    nb = snapshotted(tmp_path, code(f"%pip install packaging=={LOCAL_PIN}"), code("import packaging, resolvelib"))
    if old_baseline:  # a notebook snapshotted before the fix: its baseline holds the old heuristic
        altered(nb, lambda m: m.update(baseline=OLD_BASELINE))
    outcome = _check(nb)
    found = outcome.findings(signal="conflict") + outcome.findings(signal="yanked", package="packaging")
    assert {f["signal"] for f in found} == {"conflict", "yanked"}
    if old_baseline:  # newly visible, not new: an old notebook must not start exiting 1
        assert {f["baseline_status"] for f in found} == {"not_checked_at_generation"}


@pytest.mark.parametrize("flags, removed", [
    pytest.param("-i https://idx.test/simple", False, id="custom-index",
                 marks=known_bug("CI1", "a custom-index pin is reported removed and abandons the graph")),
    pytest.param("", True, id="removed", marks=known_bug("CI1", "a removed version abandons the graph")),
])
def test_one_unresolvable_pin_does_not_abandon_the_graph(tmp_path, pypi, flags, removed):
    pypi.add("packaging", {PACKAGING: {}})  # the pin below is not on PyPI
    pypi.add("resolvelib", {RESOLVELIB: {"requires_dist": ["demo-t>=2"]}})
    pypi.add("demo-t", {"1.0": {}})
    nb = snapshotted(tmp_path, code(f"%pip install packaging==99.0 {flags}".strip()), code("import packaging, resolvelib"))
    outcome = _check(nb)
    assert bool(outcome.findings(signal="removed", package="packaging")) is removed
    assert _confirmed(outcome, signal="conflict", package="demo-t")  # the rest of the graph is still checked


@pytest.mark.parametrize("mode", [
    pytest.param("500", marks=known_bug("LV2", "a lookup failure at snapshot is sealed into custom_sourced")),
    pytest.param("drop", marks=known_bug("LV2", "a lookup failure at snapshot is sealed into custom_sourced")),
    "404",
])
def test_custom_sourced_only_for_packages_not_on_pypi(tmp_path, pypi, mode):
    pypi.fail("packaging", mode)
    outcome = run("snapshot", Notebook(code("import packaging")).write(tmp_path), "--output")
    assert outcome.exit_code == 0, outcome.log
    assert ("packaging" in manifest(outcome.written[0])["custom_sourced"]) is (mode == "404")
