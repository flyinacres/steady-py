"""Install-line reconciliation: which install lines can undo what Cell 2 installed (conflicting, exit 1)
and which repeat it (redundant, a notice), at snapshot and at check (review_findings.md, install-lines
spec, items 5 and 10). The host's packaging is imported and pinned; resolvelib is installed, not imported."""
from importlib.metadata import version

import pytest

from tests.support.notebooks import Notebook, code
from tests.support.outcomes import manifest
from tests.support.runner import run

pytestmark = [pytest.mark.finding("DG7"), pytest.mark.usefixtures("analysis_only")]

PACKAGING = version("packaging")


def _snapshot(tmp_path, *sources, write=False):
    return run("snapshot", Notebook(*map(code, sources)).write(tmp_path), *(("--output",) if write else ()))


def _verdicts(outcome, name="packaging"):
    conflicting = [w["detail"] for w in outcome.warnings(type="conflicting_install_line", about=name)]
    redundant = [n["detail"] for n in outcome.notices(type="redundant_install_line", about=name)]
    return conflicting, redundant


@pytest.mark.parametrize("line, conflicting", [
    ("!pip install packaging", False),
    (f"!pip install packaging=={PACKAGING}", False),
    (f"!pip install -U packaging=={PACKAGING}", False),  # an exact pin can't upgrade
    ('!pip install "packaging<1"', True),  # excludes the pinned version
    ("!pip install -U packaging", True),
    ("!pip install --force-reinstall packaging", True),
    ("!uv pip install --reinstall packaging", True),
    ('!pip install "packaging @ https://files.test/packaging-1.0-py3-none-any.whl"', True),  # a direct source for an index pin
    ("%conda install packaging", True),
])
def test_each_line_is_classified(tmp_path, line, conflicting):
    outcome = _snapshot(tmp_path, f"{line}\nimport packaging")
    assert outcome.exit_code == (1 if conflicting else 0), outcome.log
    found = _verdicts(outcome)
    assert (len(found[0]), len(found[1])) == ((1, 0) if conflicting else (0, 1))


def test_a_line_installing_elsewhere_is_not_classified(tmp_path):
    outcome = _snapshot(tmp_path, "!pip install -U --target /tmp/elsewhere packaging\nimport packaging")
    assert _verdicts(outcome) == ([], [])


def test_advice_deletes_an_imported_package_and_declares_the_rest(tmp_path):
    outcome = _snapshot(tmp_path, "!pip install -U packaging\n!pip install resolvelib\nimport packaging")
    assert "Delete the line" in _verdicts(outcome)[0][0]
    assert '{"install": "resolvelib", "version": "' + version("resolvelib") + '"' in _verdicts(outcome, "resolvelib")[1][0]


def test_a_guarded_line_is_never_just_deleted(tmp_path):
    outcome = _snapshot(tmp_path, "if False:\n    !pip install -U packaging\nimport packaging")
    advice = _verdicts(outcome)[0][0]
    assert "Delete the line" not in advice and '{"ignore": "packaging"' in advice


@pytest.mark.parametrize("entry, exit_code, classified", [
    ('{"ignore": "packaging", "reason": "x"}', 0, False),
    ('{"accept": "packaging", "reason": "x"}', 0, True),
])
def test_a_declaration_overrides_the_exit(tmp_path, entry, exit_code, classified):
    outcome = _snapshot(tmp_path, f"STEADY_PY_DECLARATIONS = [{entry}]", "!pip install -U packaging\nimport packaging")
    assert outcome.exit_code == exit_code, outcome.log
    assert bool(_verdicts(outcome)[0]) is classified


def test_the_manifest_records_a_conflicting_line_left_in_place(tmp_path):
    outcome = _snapshot(tmp_path, "!pip install -U packaging\n!pip install resolvelib\nimport packaging", write=True)
    packages = {p["name"]: p for p in manifest(outcome.written[0])["packages"]}
    assert (packages["packaging"]["conflicting_line"], packages["resolvelib"]["conflicting_line"]) == (True, False)


@pytest.mark.parametrize("declared, exit_code", [("", 1), ('STEADY_PY_DECLARATIONS = [{"accept": "packaging", "reason": "x"}]', 0)])
def test_check_classifies_the_notebook_lines(tmp_path, declared, exit_code):
    cells = ([declared] if declared else []) + ["!pip install -U packaging\n!pip install resolvelib\nimport packaging"]
    written = _snapshot(tmp_path, *cells, write=True).written[0]
    outcome = run("check", written)
    assert outcome.exit_code == exit_code, outcome.log
    assert [f["severity"] for f in outcome.findings(signal="conflicting_line")] == ["confirmed"]
    assert [f["severity"] for f in outcome.findings(signal="redundant_line")] == ["notice"]
