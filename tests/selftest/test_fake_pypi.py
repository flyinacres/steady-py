"""F3 serves PyPI-shaped JSON, injects each failure as a real client sees it, and is strict."""
import http.client
import json
import urllib.error
import urllib.request
from importlib.metadata import version

import pytest

from tests.support.notebooks import Notebook, code
from tests.support.outcomes import manifest
from tests.support.runner import run


def _get(pypi, path):
    with urllib.request.urlopen(pypi.url + path, timeout=5) as resp:
        return json.loads(resp.read())


def test_project_and_version_documents(pypi):
    pypi.add("Foo_Bar", {"1.0": {"requires_dist": ["six"]}, "2.0rc1": {}, "1.1": {"yanked": True}})
    project = _get(pypi, "/pypi/foo-bar/json")
    assert project["info"]["version"] == "1.1"
    assert project["releases"]["1.1"][0]["yanked"] is True
    assert _get(pypi, "/pypi/FOO.bar/1.0.0/json")["info"]["requires_dist"] == ["six"]
    with pytest.raises(urllib.error.HTTPError, match="404"):
        _get(pypi, "/pypi/foo-bar/9.9/json")
    assert pypi.unknown == []


@pytest.mark.parametrize("mode, error", [
    ("404", urllib.error.HTTPError), ("500", urllib.error.HTTPError),
    ("drop", http.client.RemoteDisconnected), ("truncate", http.client.IncompleteRead),
])
def test_failure_modes_reach_the_client_as_real_errors(pypi, mode, error):
    pypi.add("foo", {"1.0": {}})
    pypi.fail("foo", mode, version="1.0")
    with pytest.raises(error):
        _get(pypi, "/pypi/foo/1.0/json")
    assert _get(pypi, "/pypi/foo/json")["info"]["version"] == "1.0"  # the project itself still works


def test_unregistered_lookups_are_recorded(pypi):
    with pytest.raises(urllib.error.HTTPError):
        _get(pypi, "/pypi/nobody/json")
    assert pypi.unknown == ["nobody"]
    pypi.allow_unknown()  # this test expects the lookup


def test_snapshot_validates_against_the_fake(pypi, tmp_path):
    pypi.add("packaging", {version("packaging"): {}})
    outcome = run("snapshot", Notebook(code("import packaging")).write(tmp_path), "--output")
    assert outcome.exit_code == 0, outcome.log
    assert manifest(outcome.written[0])["dependencies"] == [
        {"name": "packaging", "version": version("packaging"), "flags": []}]
