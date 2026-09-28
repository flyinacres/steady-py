"""Tier markers (default runs skip them), matrix IDs, the --findings listing, and shared fixtures."""
import re
import shutil
from collections import defaultdict
from pathlib import Path

import pytest

from tests.support import docker
from tests.support.envs import WHEELHOUSE, build_steady_py, ensure_wheelhouse, kernel_venv, steady_venv
from tests.support.fake_pypi import FakePyPI
from tests.support.markers import TIERS

_TIER_DOCS = {
    "venv": "runs the tool in a real venv (matrix layer V)",
    "kernel": "runs a real Jupyter kernel (matrix layer L)",
    "docker": "runs a Docker scenario (matrix layer D)",
}


def pytest_addoption(parser):
    parser.addoption("--findings", action="store_true",
                     help="list triage-matrix IDs with their tests and status, then exit without running")


def pytest_configure(config):
    for tier in TIERS:
        config.addinivalue_line("markers", f"{tier}: {_TIER_DOCS[tier]}; deselected unless -m selects it")
    config.addinivalue_line("markers", "finding(id): the triage-matrix ID this test covers")


def _tier(item) -> str:
    return next((t for t in TIERS if item.get_closest_marker(t)), "unit")


def pytest_collection_modifyitems(config, items):
    if config.getoption("findings") or config.getoption("markexpr"):
        return
    dropped = [item for item in items if _tier(item) != "unit"]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = [item for item in items if _tier(item) == "unit"]


def pytest_collection_finish(session):
    if not session.config.getoption("findings"):
        return
    by_id = defaultdict(list)
    for item in session.items:
        for mark in item.iter_markers("finding"):
            status = "known bug" if item.get_closest_marker("xfail") else "covered"
            by_id[mark.args[0]].append(f"{status:<10}{_tier(item):<8}{item.nodeid}")
    write = session.config.pluginmanager.get_plugin("terminalreporter").write_line
    natural = lambda s: [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s)]
    for finding_id in sorted(by_id, key=natural):
        for line in by_id[finding_id]:
            write(f"{finding_id:<6}{line}")


def pytest_runtestloop(session):
    return True if session.config.getoption("findings") else None


@pytest.fixture(scope="session")
def _pypi_server():
    server = FakePyPI()
    yield server
    server.close()


@pytest.fixture
def pypi(_pypi_server, monkeypatch):
    """The fake PyPI, empty and strict. The URL goes through the environment, so subprocesses
    (the venv tier) see it too."""
    _pypi_server.reset()
    monkeypatch.setenv("STEADY_PY_PYPI_URL", _pypi_server.url)
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    yield _pypi_server
    if _pypi_server.strict and _pypi_server.unknown:
        pytest.fail(f"lookups of unregistered projects {sorted(set(_pypi_server.unknown))}: "
                    "register them with pypi.add(), or call pypi.allow_unknown()", pytrace=False)


@pytest.fixture(scope="session")
def steady_dist(tmp_path_factory):
    """steady-py's wheel, built once per session. Skips the tier when the wheelhouse is unavailable."""
    reason = ensure_wheelhouse()
    if reason:
        pytest.skip(reason)
    return build_steady_py(tmp_path_factory.mktemp("dist"))


@pytest.fixture(scope="session")
def base_venv(steady_dist, tmp_path_factory):
    """pip, steady-py and its dependencies; shared, so tests must not install into it."""
    return steady_venv(tmp_path_factory.mktemp("venvs") / "base", steady_dist)


@pytest.fixture(scope="session")
def live_venv(steady_dist, tmp_path_factory):
    """base_venv plus ipykernel, shared by kernel-tier tests (which must not install into it).
    Skips when this process can't drive a kernel."""
    pytest.importorskip("jupyter_client", reason="the kernel tier needs jupyter_client in the test environment")
    return kernel_venv(tmp_path_factory.mktemp("venvs") / "kernel", steady_dist)


@pytest.fixture
def fresh_venv(steady_dist, tmp_path):
    """Like base_venv, but new for this test, which may install into it."""
    return steady_venv(tmp_path / "venv", steady_dist)


@pytest.fixture
def docker_work(steady_dist, tmp_path):
    """A directory to mount at /work, with steady-py's wheel and its dependencies in wheels/. Skips
    the test when Docker can't run."""
    reason = docker.unavailable()
    if reason:
        pytest.skip(reason)
    wheels = tmp_path / "work" / "wheels"
    wheels.mkdir(parents=True)
    for wheel in [*Path(steady_dist).glob("*.whl"), *WHEELHOUSE.glob("*.whl")]:
        shutil.copy2(wheel, wheels)
    return tmp_path / "work"
