"""Creator declarations: read from the notebook's cell, a declarations file or the live session,
validated, combined and recorded in the manifest (review_findings.md, install-lines spec, items 1, 2
and 4.2). What declarations do to pins, Cell 2 and exit codes is tested where those behaviors are."""
import pytest

from tests.support.kernel import run_live
from tests.support.notebooks import Notebook, code
from tests.support.outcomes import manifest
from tests.support.runner import run

pytestmark = pytest.mark.usefixtures("analysis_only")  # validation lookups aren't under test

VALID = '''STEADY_PY_DECLARATIONS = [
    {"install": "Acme_Utils", "version": "2.1.0", "extras": ["Fast"], "index_url": "https://pkgs.test/simple", "reason": "private"},
    {"ignore": "torch", "group": "gpu", "reason": "the platform supplies it"},
    {"accept": "my_module", "reason": "the runner has it"},
    {"accept": "acme-utils", "reason": "known"},
]'''


def _recorded(tmp_path, *sources, extra=()):
    outcome = run("snapshot", Notebook(*map(code, sources)).write(tmp_path), "--output", *extra)
    assert outcome.exit_code == 0, outcome.log
    return outcome, {(d["kind"], d["name"]): d for d in manifest(outcome.written[0])["declarations"]}


def test_cell_entries_are_recorded(tmp_path):
    _, recorded = _recorded(tmp_path, VALID, "import packaging")
    assert set(recorded) == {("install", "acme-utils"), ("ignore", "torch"), ("accept", "my_module"), ("accept", "acme-utils")}
    install = recorded[("install", "acme-utils")]
    assert (install["version"], install["extras"], install["options"]) == ("2.1.0", ["fast"], [["--index-url", "https://pkgs.test/simple"]])
    assert recorded[("ignore", "torch")]["group"] == "gpu"
    assert {d["origin"] for d in recorded.values()} == {"cell"}


def test_file_entries_are_recorded_and_the_cell_wins_a_conflict(tmp_path):
    declared = tmp_path / "team.py"
    declared.write_text('STEADY_PY_DECLARATIONS = [{"install": "torch", "reason": "file"}, {"ignore": "pandas", "reason": "file"}]\n')
    outcome, recorded = _recorded(tmp_path / "nb", 'STEADY_PY_DECLARATIONS = [{"ignore": "torch", "reason": "cell"}]',
                                  extra=("--declarations", str(declared)))
    assert set(recorded) == {("ignore", "torch"), ("ignore", "pandas")}
    assert recorded[("ignore", "torch")]["origin"] == "cell"
    assert recorded[("ignore", "pandas")]["origin"] == "team.py"  # its name, never its path
    assert outcome.warnings(type="declaration_conflict", about="torch")


def test_only_the_last_defining_cell_counts(tmp_path):
    outcome, recorded = _recorded(tmp_path, 'STEADY_PY_DECLARATIONS = [{"ignore": "torch", "reason": "old"}]',
                                  'STEADY_PY_DECLARATIONS = [{"ignore": "pandas", "reason": "new"}]')
    assert set(recorded) == {("ignore", "pandas")}
    assert outcome.warnings(type="declarations_cells")


def test_an_assignment_inside_a_function_is_not_read(tmp_path):
    _, recorded = _recorded(tmp_path, 'def f():\n    STEADY_PY_DECLARATIONS = [{"ignore": "torch", "reason": "x"}]')
    assert recorded == {}


@pytest.mark.parametrize("entry, problem", [
    ('{"ignore": "torch", "reson": "typo"}', "'reson'"),
    ('{"ignore": "torch"}', "reason"),
    ('{"install": "torch", "version": "==2.1", "reason": "x"}', "without an operator"),
    ('{"install": "torch", "version": "2.1", "url": "git+https://x.test/t", "reason": "x"}', "not both"),
    ('{"install": "torch", "ignore": "torch", "reason": "x"}', "exactly one"),
    ('"torch"', "must be a dict"),
])
def test_an_invalid_entry_stops_snapshot(tmp_path, entry, problem):
    outcome = run("snapshot", Notebook(code("import packaging"), code(f"STEADY_PY_DECLARATIONS = [{entry}]")).write(tmp_path))
    assert outcome.exit_code == 2
    assert "Cell 2: STEADY_PY_DECLARATIONS entry 1" in outcome.log and problem in outcome.log


def test_a_computed_literal_stops_snapshot(tmp_path):
    outcome = run("snapshot", Notebook(code("STEADY_PY_DECLARATIONS = [dict(ignore='torch', reason='x')]")).write(tmp_path))
    assert outcome.exit_code == 2
    assert "isn't a plain literal" in outcome.log


def test_an_unusable_declarations_file_stops_snapshot(tmp_path):
    declared = tmp_path / "team.py"
    declared.write_text("X = 1\n")
    outcome = run("snapshot", Notebook(code("import packaging")).write(tmp_path / "nb"), "--declarations", str(declared))
    assert outcome.exit_code == 2
    assert "team.py has no top-level STEADY_PY_DECLARATIONS" in outcome.log


def test_declarations_beside_the_manifest_get_a_warning(tmp_path):
    outcome = run("snapshot", Notebook(code("STEADY_PY_MANIFEST = {}\nSTEADY_PY_DECLARATIONS = []")).write(tmp_path))
    assert outcome.warnings(type="declarations_with_manifest")


def test_a_directory_run_fails_only_the_notebook_with_bad_declarations(tmp_path):
    Notebook(code("import packaging")).write(tmp_path, "good.ipynb")
    Notebook(code('STEADY_PY_DECLARATIONS = [{"ignore": "torch"}]')).write(tmp_path, "bad.ipynb")
    outcome = run("snapshot", str(tmp_path), "--output")
    assert outcome.exit_code == 1
    assert [p.name for p in outcome.written] == ["good_merged.ipynb"]


@pytest.mark.kernel
def test_live_session_reads_the_last_executed_version(tmp_path, live_venv):
    bad = code('STEADY_PY_DECLARATIONS = [{"ignore": "torch"}]')
    good = code('STEADY_PY_DECLARATIONS = [{"ignore": "torch", "reason": "fixed"}]')
    tmp_path.joinpath("a").mkdir(), tmp_path.joinpath("b").mkdir()
    assert run_live(live_venv, tmp_path / "a", [bad]).exit_code == 2  # control: the session's declarations are read
    assert run_live(live_venv, tmp_path / "b", [bad, good]).exit_code == 0
