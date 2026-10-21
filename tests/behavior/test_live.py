"""The live path: steady-py called from a notebook's own kernel, the Kaggle and Colab path. Each
test runs the same cells as a saved notebook (file mode, the control) and in a kernel (live mode):
both entry points should gather the same inputs and reach the same answer."""
import pytest

from tests.support.envs import installed_version
from tests.support.kernel import run_live
from tests.support.markers import finding
from tests.support.notebooks import Notebook, code
from tests.support.outcomes import manifest
from tests.support.runner import run_in

pytestmark = [pytest.mark.kernel, pytest.mark.usefixtures("analysis_only")]  # these read the analysis report


def _run(mode, venv, directory, cells):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "helper.py").write_text("")  # a real local module next to the notebook
    if mode == "live":
        outcome = run_live(venv, directory, cells)
    else:
        outcome = run_in(venv, "snapshot", Notebook(*cells).write(directory))
    assert outcome.exit_code in (0, 1), outcome.log
    return outcome


@finding("K12")
@pytest.mark.parametrize("mode", ["file", "live"])
def test_installed_package_is_pinned_and_local_module_stays_local(tmp_path, live_venv, mode):
    outcome = _run(mode, live_venv, tmp_path / "nb", [code("import packaging\nimport helper")])
    assert outcome.dependency("helper")["status"] == "local_module"  # control: local detection survives the fix
    assert outcome.pins().get("packaging") == installed_version(live_venv, "packaging")


# One cell per install form; each check is what file mode reports for it.
FORMS = {
    "pip-magic": ("%pip install resolvelib=={v} --index-url https://idx.test/simple",
                  lambda o, v: (o.dependency("resolvelib") or {}).get("flags") == ["--index-url", "https://idx.test/simple"]),
    "shell-pip": ("!pip install resolvelib=={v}", lambda o, v: o.pins().get("resolvelib") == v),
    "conda-magic": ("%conda install numpy", lambda o, v: o.notices(type="conda_command")),
    "guarded": ("if False:\n    %pip install resolvelib=={v}",
                lambda o, v: "resolvelib" not in o.pins() and o.warnings(type="guarded_install")),
    "writefile": ("%%writefile train.py\nimport resolvelib",
                  lambda o, v: (o.dependency("resolvelib") or {}).get("status") == "writefile_script"),
}


@finding("G15")
@pytest.mark.parametrize("mode", ["file", "live"])
@pytest.mark.parametrize("form", FORMS)
def test_install_forms_are_harvested(tmp_path, live_venv, mode, form):
    source, check = FORMS[form]
    version = installed_version(live_venv, "resolvelib")  # installed, so pip in the kernel is a no-op
    outcome = _run(mode, live_venv, tmp_path / "nb", [code(source.format(v=version))])
    assert check(outcome, version), outcome.dependencies()


@finding("D1")
@pytest.mark.parametrize("mode", ["file", "live"])
def test_steady_py_itself_is_not_a_dependency(tmp_path, live_venv, mode):
    outcome = _run(mode, live_venv, tmp_path / "nb", [code("import steady_py\nimport packaging")])
    assert outcome.dependency("steady_py") is None


@finding("G15")
@pytest.mark.parametrize("mode", ["file", "live"])
def test_install_lines_carry_the_location_each_mode_has(tmp_path, live_venv, mode):
    version = installed_version(live_venv, "resolvelib")
    outcome = _run(mode, live_venv, tmp_path / "nb", [code(f"%pip install resolvelib=={version}")])
    line, = outcome.install_lines()
    assert line["text"] == f"%pip install resolvelib=={version}"
    if mode == "file":
        assert line["cell_idx"] == 0
    else:  # a session has no notebook positions; the user sees In [n]
        assert line["cell_idx"] is None and line["execution_count"] > 0


# A live snapshot compares with the most recently executed Cell 2 (manifest regeneration spec, item 2.3).

def test_a_live_snapshot_compares_with_the_last_cell_2_that_ran(tmp_path, live_venv):
    written = run_in(live_venv, "snapshot", Notebook(code("import packaging")).write(tmp_path / "file"), "--output")
    assert written.exit_code == 0, written.log
    recorded = manifest(written.written[0])
    stale = dict(recorded, packages=[])  # an earlier Cell 2, run before the current one
    cells = [code(f"STEADY_PY_MANIFEST = {stale!r}"), code(f"STEADY_PY_MANIFEST = {recorded!r}"), code("import packaging")]
    outcome = _run("live", live_venv, tmp_path / "nb", cells)
    assert outcome.delta() is not None and outcome.delta()["has_changes"] is False, outcome.delta()
    assert outcome.notices(type="no_previous_manifest") == []


def test_a_live_snapshot_says_when_no_cell_2_has_run(tmp_path, live_venv):
    outcome = _run("live", live_venv, tmp_path / "nb", [code("import packaging")])
    assert outcome.delta() is None
    assert len(outcome.notices(type="no_previous_manifest")) == 1
