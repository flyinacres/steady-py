"""What Cell 2's installer does at run time: what it installs, and what it tells the runner.
Runtime summaries are text a non-engineer reads, so some assertions are on printed output."""
import pytest

from tests.support import docker
from tests.support.envs import installed_version, pip_install
from tests.support.manifests import snapshotted
from tests.support.markers import finding, known_bug
from tests.support.notebooks import code
from tests.support.outcomes import cell2_text
from tests.support.runner import install_in, write_install_inputs
from tests.support.wheels import write_wheel

RESTART = "restart the kernel"  # printed only when something was installed (development.md)


@pytest.fixture
def wheels(tmp_path):
    """The directory Cell 2 installs from, with the stub projects the runtime rows need."""
    directory = tmp_path / "wheels"
    write_wheel(directory, "demo-extras", "1.0", extras=["fast"], requires=['demo-fastdep; extra == "fast"'])
    write_wheel(directory, "demo-fastdep", "1.0")
    write_wheel(directory, "demo-a", "1.0")
    write_wheel(directory, "demo-a", "2.0")
    write_wheel(directory, "demo-b", "1.0", requires=["demo-a==2.0"])
    write_wheel(directory, "demo-loc", "1.0+cu126")
    return directory


@pytest.mark.venv
@pytest.mark.parametrize("preinstalled", [
    pytest.param("demo-extras[fast]==1.0", marks=known_bug("R1", "an extras pin never passes the installed check")),
    pytest.param("demo-extras==1.0", id="base-only"),  # the extra's dependency is missing: install it
])
def test_extras_pin_is_satisfied_only_with_the_extras_dependencies(fresh_venv, wheels, preinstalled):
    pip_install(fresh_venv, wheels, preinstalled)
    outcome = install_in(fresh_venv, ["demo-extras[fast]==1.0"], wheels)
    assert outcome.install_result()["failed"] == [], outcome.stdout
    assert installed_version(fresh_venv, "demo-fastdep") == "1.0"
    assert (RESTART in outcome.stdout) is (preinstalled == "demo-extras==1.0")


@pytest.mark.venv
@known_bug("R2", "a pin moved by a later install still counts as verified")
def test_pin_moved_by_a_later_install_is_not_verified(fresh_venv, wheels):
    outcome = install_in(fresh_venv, ["demo-a==1.0", "demo-b==1.0"], wheels)  # demo-b needs demo-a 2.0
    assert installed_version(fresh_venv, "demo-a") == "2.0"
    assert outcome.install_result()["failed"], outcome.stdout


@pytest.mark.venv
@pytest.mark.parametrize("pin, satisfied", [
    pytest.param("demo-loc==1.0", True, marks=known_bug("R3", "the installed check compares version strings")),
    pytest.param("demo-loc==1.0+cu118", False, id="other-build"),  # a local pin stays exact
])
def test_installed_check_follows_pep_440(fresh_venv, wheels, pin, satisfied):
    pip_install(fresh_venv, wheels, "demo-loc==1.0+cu126")
    outcome = install_in(fresh_venv, [pin], wheels)
    assert (outcome.install_result()["failed"] == []) is satisfied, outcome.stdout
    assert RESTART not in outcome.stdout
    assert installed_version(fresh_venv, "demo-loc") == "1.0+cu126"


@pytest.mark.venv
@known_bug("D6", "troubleshooting advice suggests an unpinned install")
def test_troubleshooting_suggests_the_pinned_version(fresh_venv, wheels):
    outcome = install_in(fresh_venv, ["demo-broken==1.0"], wheels)  # not in the wheel directory
    assert outcome.install_result()["failed"] == ["demo-broken==1.0"]
    assert "pip install demo-broken==1.0" in outcome.stdout


# A non-root user on an admin-owned Python: pip installs to the user site, which the running
# interpreter can't see until it restarts.

@pytest.mark.docker
@known_bug("R4", "a user-site install counts as verified while the old version stays in use")
def test_user_site_install_is_not_counted_as_verified(docker_work):
    write_wheel(docker_work / "wheels", "demo-a", "1.0")
    write_install_inputs(docker_work, ["demo-a==1.0"], python=docker.PYTHON)
    outcome = docker.run_as_user(docker_work, "python install.py manifest.json", setup=docker.root_pip("steady-py"))
    docker.require_scenario("Defaulting to user installation" in outcome.stdout, outcome)
    assert outcome.install_result()["installed"] == 0


@pytest.mark.docker
@pytest.mark.parametrize("preinstalled", [
    pytest.param(None, id="absent", marks=known_bug("R5", "Cell 2's helper installs but can't be imported")),
    pytest.param("steady-py==0.0.1", id="older", marks=[*known_bug("D2", "Cell 2 silently runs an older helper"),
                                                         *finding("R5")]),
])
def test_cell2_says_to_restart_when_its_helper_is_not_usable(docker_work, tmp_path, preinstalled):
    write_wheel(docker_work / "wheels", "steady-py", "0.0.1", source="def install(*args, **kwargs):\n    print('old helper')\n")
    (docker_work / "cell2.py").write_text(cell2_text(snapshotted(tmp_path, code("x = 1"))), encoding="utf-8")
    setup = docker.root_pip(preinstalled) if preinstalled else ""
    outcome = docker.run_as_user(docker_work, "python cell2.py", setup=setup)
    docker.require_scenario("Could not install the pinned" not in outcome.stdout, outcome)  # helper reached the user site
    assert RESTART in outcome.stdout, outcome.stdout + outcome.log
