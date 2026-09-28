"""The conftest hooks and known_bug behave as the design says, checked in an isolated pytest run."""
from pathlib import Path

pytest_plugins = ["pytester"]

CONFTEST = (Path(__file__).resolve().parents[1] / "conftest.py").read_text(encoding="utf-8")

SAMPLE = """
import pytest
from tests.support.markers import finding, known_bug

@known_bug("K1", "assertion")
def test_bug_asserts(): assert False

@known_bug("K2", "crash")
def test_bug_crashes(): raise TypeError

@known_bug("K10", "fixed")
def test_bug_passes(): pass

@pytest.mark.parametrize("x", [1, pytest.param(2, marks=known_bug("G4", "param"))])
def test_param(x): assert x == 1

@finding("P6")
@pytest.mark.venv
def test_venv(): pass
"""


def _run(pytester, *args):
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(SAMPLE)
    return pytester.runpytest("-p", "no:cacheprovider", *args)


def test_known_bug_is_strict_and_default_run_skips_tiers(pytester):
    result = _run(pytester)
    result.assert_outcomes(passed=1, xfailed=2, failed=2, deselected=1)
    result.stdout.fnmatch_lines(["*test_bug_crashes*TypeError*", "*XPASS(strict)*K10: fixed*"])


def test_marker_expression_selects_a_tier(pytester):
    _run(pytester, "-m", "venv").assert_outcomes(passed=1, deselected=5)


def test_findings_lists_ids_and_runs_nothing(pytester):
    result = _run(pytester, "--findings")
    result.assert_outcomes()
    result.stdout.fnmatch_lines([
        "G4 *known bug*unit*test_param?2?",
        "K1 *known bug*unit*test_bug_asserts",
        "K2 *known bug*unit*test_bug_crashes",
        "K10 *known bug*unit*test_bug_passes",  # natural order: K2 before K10
        "P6 *covered*venv*test_venv",
    ], consecutive=True)
