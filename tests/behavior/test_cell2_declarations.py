"""What Cell 2 does with declarations and kept install lines (review_findings.md, install-lines spec,
item 6): ignored packages are reported and not counted, and a conflicting line the author kept is named.
Every pin here is already installed, so steady_py.install() runs in-process without pip."""
from importlib.metadata import version

import pytest

import steady_py
from tests.support.notebooks import Notebook, code
from tests.support.outcomes import manifest
from tests.support.runner import run

pytestmark = pytest.mark.usefixtures("analysis_only")


def _written(tmp_path, *sources):
    outcome = run("snapshot", Notebook(*map(code, sources)).write(tmp_path), "--output")
    assert outcome.exit_code == 0, outcome.log
    return outcome


def _cell2(tmp_path, capsys, *sources):
    result = steady_py.install(manifest(_written(tmp_path, *sources).written[0]))
    return result, capsys.readouterr().out


@pytest.mark.parametrize("entry, shown, total", [
    ('{"ignore": "resolvelib", "reason": "x"}', f"resolvelib: {version('resolvelib')} is installed", 1),
    ('{"ignore": "resolvelib", "version": "0.1", "reason": "x"}', "the notebook was made with 0.1", 1),
    ('{"ignore": "not-a-real-package", "reason": "x"}', "not-a-real-package: not installed yet", 2),
])
def test_an_ignored_package_is_reported_and_not_counted(tmp_path, capsys, entry, shown, total):
    result, out = _cell2(tmp_path, capsys, f"STEADY_PY_DECLARATIONS = [{entry}]", "import packaging, resolvelib")
    assert shown in out
    assert (result.total, result.ok) == (total, True)  # an ignored package isn't one of the N


def test_a_kept_conflicting_line_is_named(tmp_path, capsys):
    _, out = _cell2(tmp_path, capsys, 'STEADY_PY_DECLARATIONS = [{"accept": "packaging", "reason": "x"}]',
                    "!pip install -U packaging\nimport packaging")
    assert "A later cell in this notebook can change: packaging" in out


def test_without_declarations_cell2_says_nothing_more(tmp_path, capsys):  # control for the two above
    _, out = _cell2(tmp_path, capsys, "import packaging")
    assert "not by this cell" not in out and "A later cell" not in out


def test_snapshot_warns_when_a_pin_depends_on_an_ignored_package(tmp_path):
    outcome = _written(tmp_path, 'STEADY_PY_DECLARATIONS = [{"ignore": "pluggy", "reason": "x"}]', "import pytest")
    assert outcome.warnings(type="ignored_reachable", about="pluggy")
