"""Round trip (review_findings.md, install-lines spec, items 3, 5.4, 5.6 and 7): snapshot proposes
declarations for install lines Cell 2 doesn't reproduce, ready to paste, and pasting them settles the
line; advice and proposals print as one list; packages.guard records a guarded line; the author
banner leads snapshot's text output; the live helper shows the report and both cells (D7)."""
import ast
from importlib.metadata import version

import pytest

import steady_py
from tests.support.kernel import kernel
from tests.support.markers import finding
from tests.support.notebooks import Notebook, code
from tests.support.outcomes import manifest
from tests.support.runner import run

pytestmark = pytest.mark.usefixtures("analysis_only")

PACKAGING = version("packaging")
ALTERNATIVES = "import sys\nif 'google.colab' in sys.modules:\n    !pip install colab-only-pkg\nelse:\n    !pip install local-only-pkg"
BANNER = "This output is for the notebook's author."


def _snapshot(tmp_path, *sources, extra=()):
    return run("snapshot", Notebook(*map(code, sources)).write(tmp_path), *extra)


def _proposals(outcome):
    return outcome.notices(type="declaration_proposal")


def _declared(*entries):
    return f"STEADY_PY_DECLARATIONS = [{', '.join(entries)}]"


@finding("DG2")
def test_guarded_alternatives_are_proposed_as_ignores_sharing_a_group(tmp_path):
    outcome = _snapshot(tmp_path, ALTERNATIVES)
    entries = [ast.literal_eval(e) for p in _proposals(outcome) for e in p["entries"]]
    assert [e["ignore"] for e in entries] == ["colab-only-pkg", "local-only-pkg"]
    assert entries[0]["group"] == entries[1]["group"]


@finding("DG2")
def test_pasting_the_proposals_settles_the_lines(tmp_path):
    first = _snapshot(tmp_path / "a", ALTERNATIVES, "!pip install not-here-pkg==1.0")
    pasted = [e for p in _proposals(first) for e in p["entries"] if '"ignore": "not-here' not in e]
    second = _snapshot(tmp_path / "b", _declared(*pasted), ALTERNATIVES, "!pip install not-here-pkg==1.0",
                       extra=("--output",))
    assert second.exit_code == 0, second.log
    assert _proposals(second) == []
    recorded = manifest(second.written[0])
    assert {d["name"]: d.get("group") for d in recorded["declarations"] if d["kind"] == "ignore"}.keys() == {"colab-only-pkg", "local-only-pkg"}
    assert {p["name"]: p["version"] for p in recorded["packages"]}["not-here-pkg"] == "1.0"
    assert "your install declaration keeps the pin" in second.notices(type="redundant_install_line")[0]["detail"]


@pytest.mark.parametrize("line, entry", [
    ("!pip install not-here-pkg==1.0", '{"install": "not-here-pkg", "version": "1.0", "reason": "..."}'),
    ("!conda install -y not-here-pkg", '{"ignore": "not-here-pkg", "reason": "..."}'),
    ("if False:\n    !pip install 'packaging[fast]'", f'{{"install": "packaging", "version": "{PACKAGING}", "extras": ["fast"], "reason": "..."}}'),
])
def test_each_kind_of_line_gets_its_entry(tmp_path, line, entry):
    outcome = _snapshot(tmp_path, f"{line}\nimport packaging")
    assert entry in [e for p in _proposals(outcome) for e in p["entries"]]


@pytest.mark.parametrize("line", ["pkg = 'x'\n%pip install $pkg", "!pip install -r requirements.txt"])
def test_an_unknown_target_gets_a_template_that_cannot_pass_unedited(tmp_path, line):
    template = _proposals(_snapshot(tmp_path / "a", line))[0]["entries"][0]
    assert _snapshot(tmp_path / "b", _declared(template), line).exit_code == 2


def test_a_declared_package_gets_no_proposal(tmp_path):  # control for the proposals above
    outcome = _snapshot(tmp_path, _declared('{"ignore": "not-here-pkg", "reason": "x"}'), "!conda install -y not-here-pkg")
    assert _proposals(outcome) == []


@pytest.mark.parametrize("line, guard", [
    ("try:\n    import packaging\nexcept ImportError:\n    !pip install packaging", {"kind": "except", "branch": 1}),
    ("!pip install packaging\nimport packaging", None),
])
def test_packages_guard_records_a_guarded_install_line(tmp_path, line, guard):
    outcome = _snapshot(tmp_path, line, extra=("--output",))
    recorded = {p["name"]: p["guard"] for p in manifest(outcome.written[0])["packages"]}["packaging"]
    assert (recorded and {k: recorded[k] for k in ("kind", "branch")}) == guard


def test_advice_and_proposals_print_as_one_list_after_the_banner(tmp_path):
    outcome = _snapshot(tmp_path, "!pip install -U packaging\nimport packaging", "!conda install -y not-here-pkg",
                        extra=("--format", "text"))
    assert outcome.log.startswith(BANNER)
    listed = outcome.log.split("INSTALL LINES")[1].split("\n\n")[0]
    assert listed.index("-U packaging") < listed.index("conda install")
    assert outcome.log.count("-U packaging` can change") == 1  # not repeated among the warnings
    assert BANNER not in _snapshot(tmp_path / "json", "import packaging").stdout


def test_an_ignored_alternative_is_reported_as_one_of_its_group(tmp_path, capsys):
    outcome = _snapshot(tmp_path, _declared('{"ignore": "not-here-pkg", "group": "gpu", "reason": "x"}'),
                        "import packaging", extra=("--output",))
    steady_py.install(manifest(outcome.written[0]))
    out = capsys.readouterr().out
    assert "not-here-pkg: not installed (one of the alternatives in group 'gpu')" in out
    assert "a later cell installs it" not in out


@pytest.mark.kernel
@finding("D7")
@pytest.mark.parametrize("run_type, shown", [("Interactive", True), ("Batch", False)])
def test_the_live_helper_shows_the_report_and_cells_except_in_kaggle_save_version(tmp_path, live_venv, run_type, shown):
    with kernel(live_venv, tmp_path, env={"KAGGLE_KERNEL_RUN_TYPE": run_type}) as k:
        k.execute("!pip install -U packaging\nimport packaging")
        out, err = k.execute("import steady_py\nsteady_py.show_snapshot()")
    assert not err.strip() or "Traceback" not in err, err
    parts = [BANNER, "INSTALL LINES", "STEP 1: PASTE INTO CELL 1", "STEP 2: PASTE INTO CELL 2", "STEADY_PY_MANIFEST"]
    assert [p in out for p in parts] == [shown] * len(parts), out
    assert ("Save Version" in out) is not shown
