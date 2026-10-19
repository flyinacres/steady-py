"""Snapshot's analysis of the corpus fixtures, against a recorded golden file.

The fixtures (tests/fixtures/corpus, built by tools/corpus_fixtures.py) are 33 real notebooks
chosen to cover every install-line form and analysis signal in the corpus. The golden file is
their analysis in tools/scan_diff.py's canonical form. Pins and statuses depend on the OS, the
Python version and what is installed, so it is made where CI's corpus step runs: Linux, Python
3.13, the dev lock (-m corpus, Ubuntu only). STEADY_PY_UPDATE_GOLDEN=1 rewrites it instead of
comparing; a delivery that changes harvesting regenerates it, and its diff is the review.
"""
import importlib.util
import json
import os
from collections import Counter
from pathlib import Path

import pytest

from tests.support.markers import unavailable
from tests.support.runner import run

pytestmark = [pytest.mark.corpus, pytest.mark.usefixtures("analysis_only")]

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "tests" / "fixtures" / "corpus"
GOLDEN = REPO / "tests" / "fixtures" / "corpus_expected.json"


def _canonical(report: dict) -> dict:
    spec = importlib.util.spec_from_file_location("scan_diff", REPO / "tools" / "scan_diff.py")
    scan_diff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scan_diff)
    return scan_diff.canonical(report)


def _differences(expected: dict, actual: dict) -> list:
    lines = []
    for path in sorted(expected.keys() | actual.keys()):
        if path not in actual or path not in expected:
            lines.append(f"{path}: only in {'golden' if path in expected else 'this run'}")
            continue
        for field in sorted(expected[path].keys() | actual[path].keys()):
            was, now = Counter(expected[path].get(field, [])), Counter(actual[path].get(field, []))
            lines += [f"{path}: - {field}: {item}" for item in sorted((was - now).elements())]
            lines += [f"{path}: + {field}: {item}" for item in sorted((now - was).elements())]
    return lines


def test_the_corpus_analysis_matches_the_golden_file():
    if not any(CORPUS.rglob("*.ipynb")):
        unavailable("no corpus fixtures: run tools/corpus_fixtures.py and commit tests/fixtures/corpus")
    outcome = run("snapshot", CORPUS)
    assert outcome.exit_code in (0, 1), outcome.log
    actual = _canonical(outcome.report)
    if os.environ.get("STEADY_PY_UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return
    if not GOLDEN.is_file():
        pytest.fail(f"no golden file at {GOLDEN.relative_to(REPO).as_posix()}: generate it on Linux with "
                    f"Python 3.13 and the dev lock, with STEADY_PY_UPDATE_GOLDEN=1", pytrace=False)
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    differences = _differences(expected, actual)
    assert not differences, "the corpus analysis changed:\n" + "\n".join(differences)
