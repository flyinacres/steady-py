"""The endpoints compute typed results; they never print or exit. check is the first."""
import json

import pytest

import steady_py.core as spy
from steady_py.endpoints import check
from steady_py.results import CheckOptions, TargetKind

DEPS = [spy.PinnedDependency("requests", "2.32.1")]


@pytest.fixture
def offline(monkeypatch):
    """No PyPI: pin checks return nothing unless a test says otherwise."""
    monkeypatch.setattr(spy, "run_pin_checks", lambda deps, python_version: [])


def _notebook(tmp_path, source, name="nb.ipynb"):
    cell = {"cell_type": "code", "source": [source], "metadata": {}, "outputs": [], "execution_count": None}
    path = tmp_path / name
    path.write_text(json.dumps({"cells": [cell], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}), encoding="utf-8")
    return path


def _notebook_with_manifest(tmp_path, deps=DEPS):
    return _notebook(tmp_path, spy.generate_production_blueprint(deps)["step2_code"])


class TestCheck:
    def test_returns_one_file_result_for_a_notebook_with_a_manifest(self, tmp_path, offline):
        path = _notebook_with_manifest(tmp_path)
        result = check(str(path))
        assert (result.target, result.kind, len(result.notebooks)) == (str(path), TargetKind.FILE, 1)
        notebook = result.notebooks[0]
        assert notebook.manifest_found and notebook.error is None
        assert notebook.report.manifest.dependencies == DEPS
        assert notebook.report.is_clean

    def test_no_manifest_is_not_an_error(self, tmp_path, offline):
        notebook = check(str(_notebook(tmp_path, "import requests"))).notebooks[0]
        assert (notebook.manifest_found, notebook.report, notebook.error) == (False, None, None)

    def test_an_unreadable_file_is_an_error(self, tmp_path, offline):
        notebook = check(str(tmp_path / "missing.ipynb")).notebooks[0]
        assert notebook.error and not notebook.manifest_found and notebook.report is None

    def test_a_hand_edited_manifest_is_reported_as_tampering(self, tmp_path, offline):
        path = _notebook_with_manifest(tmp_path)
        edited = path.read_text(encoding="utf-8").replace("'2.32.1'", "'2.32.0'")
        assert edited != path.read_text(encoding="utf-8")
        path.write_text(edited, encoding="utf-8")
        report = check(str(path)).notebooks[0].report
        assert [f.signal for f in report.confirmed] == [spy.Signal.TAMPERED]

    def test_pin_checks_run_on_the_manifests_pins_and_python(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(spy, "run_pin_checks", lambda deps, python_version: seen.append((deps, python_version)) or [])
        path = _notebook_with_manifest(tmp_path)
        check(str(path))
        (deps, python_version), = seen[-1:]
        assert deps == DEPS
        assert python_version == {"major": spy.sys.version_info.major, "minor": spy.sys.version_info.minor}

    def test_findings_from_the_pin_checks_land_in_the_report(self, tmp_path, monkeypatch):
        finding = spy.DriftFinding("requests", "2.32.1", spy.Signal.YANKED, spy.Severity.CONFIRMED, "yanked")
        path = _notebook_with_manifest(tmp_path)
        monkeypatch.setattr(spy, "run_pin_checks", lambda deps, python_version: [finding])
        assert check(str(path)).notebooks[0].report.confirmed == [finding]

    def test_root_dir_and_notebook_dir_reach_the_local_module_check(self, tmp_path, monkeypatch, offline):
        seen = {}
        monkeypatch.setattr(spy, "check_local_modules", lambda manifest, notebook_dir, root_dir=None: seen.update(nb=notebook_dir, root=root_dir) or [])
        path = _notebook_with_manifest(tmp_path)
        check(str(path), CheckOptions(root_dir="/repo"))
        assert seen == {"nb": str(tmp_path), "root": "/repo"}

    def test_computes_without_printing(self, tmp_path, offline, capsys):
        check(str(_notebook_with_manifest(tmp_path)))
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""
