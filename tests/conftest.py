"""Tier markers (default runs skip them), matrix IDs, the --findings listing, and shared fixtures."""
import re
from collections import defaultdict

import pytest

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
