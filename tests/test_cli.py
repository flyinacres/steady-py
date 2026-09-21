"""The CLI layer: formatting and exit-code mapping are pure functions of a result; run_check ties
them to the endpoint and to stdout/stderr."""
import json

import pytest

import steady_py.cli as cli
import steady_py.core as spy
from steady_py.results import CheckResult, NotebookCheck, TargetKind

PIN = spy.PinnedDependency("requests", "2.32.1")


def _manifest(baseline=None):
    return spy.SteadyPyManifest(python_version={"major": 3, "minor": 12}, dependencies=[PIN], gpu=None,
                                generated_at="t", baseline=baseline)


def _finding(signal, severity, **kw):
    return spy.DriftFinding("requests", "2.32.1", signal, severity, "message", **kw)


def _checked(*findings, baseline=None, path="a.ipynb"):
    manifest = _manifest(baseline)
    return NotebookCheck(path=path, manifest_found=True, report=spy.build_drift_check_report(path, manifest, list(findings)))


def _result(*notebooks):
    return CheckResult(target="a.ipynb", kind=TargetKind.FILE, notebooks=list(notebooks))


class TestCheckExitCode:
    def test_clean_is_0(self):
        assert cli.check_exit_code(_result(_checked())) == 0

    def test_no_manifest_is_0(self):
        assert cli.check_exit_code(_result(NotebookCheck(path="a.ipynb"))) == 0

    def test_a_confirmed_finding_is_1(self):
        assert cli.check_exit_code(_result(_checked(_finding(spy.Signal.YANKED, spy.Severity.CONFIRMED)))) == 1

    def test_a_confirmed_finding_already_known_at_generation_still_counts(self):
        baseline = spy.Baseline(findings=(("yanked", "requests", "2.32.1"),))
        result = _result(_checked(_finding(spy.Signal.YANKED, spy.Severity.CONFIRMED), baseline=baseline))
        assert cli.check_exit_code(result) == 1

    def test_a_new_heuristic_finding_is_1(self):
        assert cli.check_exit_code(_result(_checked(_finding(spy.Signal.STALE, spy.Severity.HEURISTIC)))) == 1

    def test_a_heuristic_finding_already_known_at_generation_is_0(self):
        baseline = spy.Baseline(findings=(("stale", "requests"),))
        result = _result(_checked(_finding(spy.Signal.STALE, spy.Severity.HEURISTIC), baseline=baseline))
        assert cli.check_exit_code(result) == 0

    def test_a_pin_that_could_not_be_checked_is_2(self):
        assert cli.check_exit_code(_result(_checked(_finding(spy.Signal.CHECK_ERROR, spy.Severity.ERROR)))) == 2

    def test_an_unreadable_manifest_is_2(self):
        assert cli.check_exit_code(_result(NotebookCheck(path="a.ipynb", error="not a manifest"))) == 2

    def test_errors_outrank_drift(self):
        drift = _checked(_finding(spy.Signal.YANKED, spy.Severity.CONFIRMED))
        result = _result(drift, NotebookCheck(path="b.ipynb", error="unreadable"))
        assert cli.check_exit_code(result) == 2

    def test_no_notebooks_is_0(self):
        assert cli.check_exit_code(_result()) == 0


class TestFormatCheckResult:
    def test_no_manifest_says_nothing_to_check_on_stdout(self):
        out, err = cli.format_check_result(_result(NotebookCheck(path="a.ipynb")))
        assert (out, err) == ("No STEADY_PY_MANIFEST found in a.ipynb -- nothing to check.", "")

    def test_an_error_goes_to_stderr(self):
        out, err = cli.format_check_result(_result(NotebookCheck(path="a.ipynb", error="boom")))
        assert (out, err) == ("", "⚠️ boom")

    def test_text_is_the_console_drift_report(self):
        checked = _checked(_finding(spy.Signal.YANKED, spy.Severity.CONFIRMED))
        out, err = cli.format_check_result(_result(checked))
        assert out == spy.format_console_drift_report(checked.report) and err == ""

    def test_json_is_the_json_drift_report(self):
        checked = _checked()
        out, _ = cli.format_check_result(_result(checked), "json")
        assert out == spy.format_json_drift_report(checked.report)
        assert json.loads(out)["target"] == "a.ipynb"


class TestRunCheck:
    @pytest.fixture(autouse=True)
    def offline(self, monkeypatch):
        monkeypatch.setattr(spy, "run_pin_checks", lambda deps, python_version: [])

    def _write(self, tmp_path, source):
        cell = {"cell_type": "code", "source": [source], "metadata": {}, "outputs": [], "execution_count": None}
        path = tmp_path / "nb.ipynb"
        path.write_text(json.dumps({"cells": [cell], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}), encoding="utf-8")
        return str(path)

    def test_prints_the_report_and_returns_the_code(self, tmp_path, capsys):
        path = self._write(tmp_path, spy.generate_production_blueprint([PIN])["step2_code"])
        assert cli.run_check(path) == 0
        assert capsys.readouterr().out.strip() != ""

    def test_no_manifest_prints_the_message_and_returns_0(self, tmp_path, capsys):
        path = self._write(tmp_path, "import requests")
        assert cli.run_check(path) == 0
        assert "nothing to check" in capsys.readouterr().out

    def test_an_unreadable_file_writes_to_stderr_and_returns_2(self, tmp_path, capsys):
        assert cli.run_check(str(tmp_path / "missing.ipynb")) == 2
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err.strip() != ""
