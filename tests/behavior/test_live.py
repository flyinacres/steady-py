"""The live path: steady-py called from a notebook's own kernel, the Kaggle and Colab path. Each
test runs the same cells as a saved notebook (file mode, the control) and in a kernel (live mode):
both entry points should gather the same inputs and reach the same answer."""
import pytest

from tests.support.envs import installed_version
from tests.support.kernel import run_live
from tests.support.markers import finding, known_bug
from tests.support.notebooks import Notebook, code
from tests.support.runner import run_in

pytestmark = pytest.mark.kernel


def _run(mode, venv, directory, cells):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "helper.py").write_text("")  # a real local module next to the notebook
    if mode == "live":
        outcome = run_live(venv, directory, cells)
    else:
        outcome = run_in(venv, "scan", Notebook(*cells).write(directory))
    assert outcome.exit_code in (0, 1), outcome.log
    return outcome


def _modes(finding_id, why):
    return pytest.mark.parametrize("mode", ["file", pytest.param("live", marks=known_bug(finding_id, why))])


@_modes("K12", "any importable module is a local module in a live kernel, so nothing installed is pinned")
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
    "writefile": ("%%writefile train.py\nimport resolvelib",
                  lambda o, v: (o.dependency("resolvelib") or {}).get("status") == "writefile_script"),
}


def _form_modes():
    # The live pin also needs K12: today any importable module is a local module in a live kernel.
    k12 = known_bug("K12", "the harvested install is pinned as a local module in a live kernel")
    return [pytest.param(form, mode, marks=k12 if (form, mode) == ("shell-pip", "live") else ())
            for form in FORMS for mode in ("file", "live")]


@finding("G15")
@pytest.mark.parametrize(("form", "mode"), _form_modes())
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
