"""What the generated setup runs: install lines steady-py can't pin are carried verbatim and installed
after the pins; install() installs, skips what's satisfied, warns on a Python mismatch and reports
failures without stopping or raising; and Cell 2 itself runs unmodified from local wheels."""
import shutil
from importlib.metadata import version

import pytest

from tests.support.envs import WHEELHOUSE, create_venv, installed_version, pip_install
from tests.support.manifests import altered, snapshotted
from tests.support.notebooks import Notebook, code
from tests.support.outcomes import manifest
from tests.support.runner import install_in, run, run_cell2
from tests.support.wheels import write_wheel

PACKAGING = version("packaging")
RESTART = "restart the kernel"  # printed only when something was installed (development.md)


@pytest.mark.parametrize("line, spec", [
    ("%pip install git+https://example.com/org/demo.git@v1.2#egg=demo", "git+https://example.com/org/demo.git@v1.2#egg=demo"),
    ("!pip install -q git+https://example.com/org/demo.git@0123abc", "git+https://example.com/org/demo.git@0123abc"),
])
def test_a_vcs_install_line_is_carried_verbatim(tmp_path, pypi, line, spec):
    pypi.add("packaging", {PACKAGING: {}})
    outcome = run("snapshot", Notebook(code(line), code("import packaging")).write(tmp_path), "--output")
    assert outcome.exit_code == 0, outcome.log
    written = manifest(outcome.written[0])
    assert written["raw_installs"] == [{"spec": spec, "flags": []}]
    assert [d["name"] for d in written["dependencies"]] == ["packaging"]


@pytest.fixture
def wheels(tmp_path):
    directory = tmp_path / "wheels"
    write_wheel(directory, "demo-a", "1.0")
    write_wheel(directory, "demo-c", "1.0")
    return directory


@pytest.mark.venv
def test_raw_installs_run_after_the_pins(fresh_venv, wheels):
    raw = str(write_wheel(wheels, "demo-raw", "1.0"))  # the installer passes any raw spec to pip as is
    outcome = install_in(fresh_venv, ["demo-a==1.0"], wheels, raw_installs=[raw])
    assert outcome.install_result() == {"total": 2, "installed": 2, "failed": []}, outcome.stdout
    assert installed_version(fresh_venv, "demo-raw") == "1.0"
    assert outcome.stdout.index("demo-a==1.0") < outcome.stdout.index(raw)


@pytest.mark.venv
def test_install_installs_each_pin(fresh_venv, wheels):
    outcome = install_in(fresh_venv, ["demo-a==1.0", "demo-c==1.0"], wheels)
    assert outcome.install_result() == {"total": 2, "installed": 2, "failed": []}, outcome.stdout
    assert [installed_version(fresh_venv, n) for n in ("demo-a", "demo-c")] == ["1.0", "1.0"]
    assert RESTART in outcome.stdout


@pytest.mark.venv
def test_install_skips_a_pin_already_satisfied(fresh_venv, wheels):
    pip_install(fresh_venv, wheels, "demo-a==1.0")
    outcome = install_in(fresh_venv, ["demo-a==1.0"], wheels)
    assert outcome.install_result() == {"total": 1, "installed": 1, "failed": []}, outcome.stdout
    assert RESTART not in outcome.stdout  # nothing was installed


@pytest.mark.venv
def test_install_warns_on_a_python_mismatch_and_continues(fresh_venv, wheels):
    outcome = install_in(fresh_venv, ["demo-a==1.0"], wheels, python=(2, 7))
    assert "2.7" in outcome.stdout
    assert outcome.install_result()["failed"] == [], outcome.stdout


@pytest.mark.venv
def test_install_reports_failures_without_stopping_or_raising(fresh_venv, wheels, tmp_path):
    missing_raw = str(tmp_path / "absent-1.0-py3-none-any.whl")
    outcome = install_in(fresh_venv, ["demo-missing==1.0", "demo-a==1.0"], wheels, raw_installs=[missing_raw])
    assert outcome.exit_code == 0, outcome.log
    assert outcome.install_result() == {"total": 3, "installed": 1, "failed": ["demo-missing==1.0", missing_raw]}
    assert installed_version(fresh_venv, "demo-a") == "1.0"


@pytest.mark.venv
def test_cell2_runs_unmodified_from_local_wheels(tmp_path, pypi, steady_dist, wheels):
    for wheel in [*steady_dist.glob("*.whl"), *WHEELHOUSE.glob("*.whl")]:
        shutil.copy2(wheel, wheels)
    pypi.add("packaging", {PACKAGING: {}})
    nb = altered(snapshotted(tmp_path, code("import packaging")),
                 lambda m: m.update(dependencies=[{"name": "demo-a", "version": "1.0", "flags": []}]))
    venv = create_venv(tmp_path / "venv")  # no steady-py: Cell 2 installs its own helper
    outcome = run_cell2(venv, nb, wheels)
    assert outcome.exit_code == 0, outcome.stdout + outcome.log
    assert installed_version(venv, "steady-py") == manifest(nb)["tool_version"]
    assert installed_version(venv, "demo-a") == "1.0"
