"""What check reports when PyPI moves after a snapshot: each signal, its severity, how it's classified
against the generation-time baseline, and the exit code that follows. Exit 2 (a pin that couldn't be
checked) is owned by tests/behavior/test_validation.py::test_dropped_connection_is_a_network_error."""
from importlib.metadata import version

import pytest

from tests.support.manifests import altered, snapshotted
from tests.support.notebooks import code
from tests.support.runner import run

PACKAGING, RESOLVELIB = version("packaging"), version("resolvelib")
NEXT_MAJOR = f"{int(PACKAGING.split('.')[0]) + 1}.0"
LONG_AGO = "2020-01-01T00:00:00.000000Z"  # past the two-year staleness threshold

MOVES = {  # what PyPI does to the pinned packaging release after the snapshot
    "yanked": lambda pypi: pypi.add("packaging", {PACKAGING: {"yanked": True, "yanked_reason": "bad build"}}),
    "removed": lambda pypi: pypi.add("packaging", {"1.0": {}}),
    "unsupported_python": lambda pypi: pypi.add("packaging", {PACKAGING: {"requires_python": ">=4"}}),
    "stale": lambda pypi: pypi.add("packaging", {PACKAGING: {"upload_time": LONG_AGO}}),
    "major_bump": lambda pypi: pypi.add("packaging", {PACKAGING: {}, NEXT_MAJOR: {}}),
}
SEVERITY = {"yanked": "confirmed", "removed": "confirmed", "unsupported_python": "confirmed",
            "stale": "heuristic", "major_bump": "heuristic"}


def _check(path, exit_code):
    outcome = run("check", path)
    assert outcome.exit_code == exit_code, outcome.log
    return outcome


def _classified(outcome, signal):
    return [(f["severity"], f.get("baseline_status")) for f in outcome.findings(signal=signal)]


@pytest.mark.parametrize("signal", MOVES)
def test_drift_after_snapshot_is_new_and_fails_the_check(tmp_path, pypi, signal):
    pypi.add("packaging", {PACKAGING: {}})
    nb = snapshotted(tmp_path, code("import packaging"))
    MOVES[signal](pypi)
    outcome = _check(nb, 1)
    assert _classified(outcome, signal) == [(SEVERITY[signal], "new")]
    assert outcome.report["baseline"]["new"] >= 1


def test_a_new_conflict_fails_the_check(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    pypi.add("resolvelib", {RESOLVELIB: {}})
    nb = snapshotted(tmp_path, code("import packaging, resolvelib"))
    pypi.add("resolvelib", {RESOLVELIB: {"requires_dist": [f"packaging<{PACKAGING}"]}})
    outcome = _check(nb, 1)
    assert set(_classified(outcome, "conflict")) == {("confirmed", "new")}


def test_a_known_confirmed_finding_still_fails_the_check(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    pypi.add("resolvelib", {RESOLVELIB: {"requires_dist": [f"packaging<{PACKAGING}"]}})
    nb = snapshotted(tmp_path, code("import packaging, resolvelib  # conflict at snapshot"))
    outcome = _check(nb, 1)
    assert set(_classified(outcome, "conflict")) == {("confirmed", "known")}


@pytest.mark.parametrize("signal", ["stale", "major_bump"])
def test_a_known_heuristic_finding_passes_the_check(tmp_path, pypi, signal):
    MOVES[signal](pypi)
    nb = snapshotted(tmp_path, code(f"import packaging  # {signal} at snapshot"))
    outcome = _check(nb, 0)
    assert _classified(outcome, signal) == [("heuristic", "known")]


@pytest.mark.parametrize("signal", ["yanked", "major_bump"])
def test_a_package_unchecked_at_generation_gets_no_new_or_known_claim(tmp_path, pypi, signal):
    pypi.add("packaging", {PACKAGING: {}})
    nb = altered(snapshotted(tmp_path, code("import packaging")), lambda m: m["baseline"].update(errors=["packaging"]))
    MOVES[signal](pypi)
    outcome = _check(nb, 1)  # not known, so a heuristic fails too
    assert _classified(outcome, signal) == [(SEVERITY[signal], "not_checked_at_generation")]


def test_without_a_baseline_every_heuristic_finding_fails(tmp_path, pypi):
    pypi.add("packaging", {PACKAGING: {}})
    nb = altered(snapshotted(tmp_path, code("import packaging")), lambda m: m.update(baseline=None))
    MOVES["major_bump"](pypi)
    outcome = _check(nb, 1)
    assert outcome.report["baseline"] == {"recorded": False}
    assert _classified(outcome, "major_bump") == [("heuristic", None)]


@pytest.mark.parametrize("on_pypi_at_snapshot", [False, True], ids=["private", "vanished"])
def test_a_known_private_package_is_a_notice_but_a_vanished_one_fails(tmp_path, pypi, on_pypi_at_snapshot):
    if on_pypi_at_snapshot:
        pypi.add("packaging", {PACKAGING: {}})
    else:
        pypi.fail("packaging", "404")
    nb = snapshotted(tmp_path, code(f"import packaging  # on PyPI at snapshot: {on_pypi_at_snapshot}"))
    pypi.fail("packaging", "404")
    outcome = _check(nb, 1 if on_pypi_at_snapshot else 0)
    expected = ("confirmed", "new") if on_pypi_at_snapshot else ("notice", "known")
    assert _classified(outcome, "not_found_on_pypi") == [expected]
