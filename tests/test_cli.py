"""The CLI layer: formatting and exit-code mapping are pure functions of a result; run_check ties
them to the endpoint and to stdout/stderr."""
import argparse
import json
import logging

import pytest

import steady_py.cli as cli
import steady_py.core as spy
import steady_py.endpoints as endpoints
from steady_py.results import (
    CheckResult, Environment, NotebookCheck, NotebookScan, NotebookSnapshot, ScanResult, SetupCells,
    SnapshotResult, TargetKind, WriteMode,
)

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


# ---------------------------------------------------------------------------------------------
# scan and snapshot for one notebook

ENV = Environment(frozen_env={"requests": "requests==2.32.3"}, pkg_dist_map={"requests": ["requests"]})


def _args(**overrides):
    """The namespace main() builds, with every flag at its default."""
    values = dict(notebook=None, format="text", full_freeze=False, timeout=120, quiet=False, verbose=False,
                  check_drift=False, root_dir=None, batch=None, analyze=False, universal=None, output=False,
                  output_dir=None, suffix=None, in_place=False)
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(spy, "run_pin_checks", lambda deps, python_version: [])
    monkeypatch.setattr(spy, "inspect_gpu_environment", lambda imports: None)
    monkeypatch.setattr(endpoints, "detect_environment", lambda: ENV)


def _write_notebook(tmp_path, source="import requests", name="nb.ipynb"):
    cell = {"cell_type": "code", "source": [source], "metadata": {}, "outputs": [], "execution_count": None}
    path = tmp_path / name
    path.write_text(json.dumps({"cells": [cell], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}), encoding="utf-8")
    return str(path)


def _unreadable(tmp_path):
    path = tmp_path / "bad.ipynb"
    path.write_text("{ not json", encoding="utf-8")
    return str(path)


class TestScanAndSnapshotExitCodes:
    def test_success_is_0_and_an_unreadable_notebook_is_1(self):
        report = spy.NotebookAnalysisReport(notebook_path="a.ipynb", is_python=True, lang_label="python")
        good, bad = NotebookScan(path="a", report=report), NotebookScan(path="b", report=report, error="boom")
        assert cli.scan_exit_code(ScanResult(target="a", notebooks=[good])) == 0
        assert cli.scan_exit_code(ScanResult(target="b", notebooks=[bad])) == 1
        good_s, bad_s = NotebookSnapshot(path="a", report=report), NotebookSnapshot(path="b", report=report, error="boom")
        assert cli.snapshot_exit_code(SnapshotResult(target="a", notebooks=[good_s])) == 0
        assert cli.snapshot_exit_code(SnapshotResult(target="b", notebooks=[bad_s])) == 1


class TestWriteModeFromFlags:
    @pytest.mark.parametrize("flags, expected", [
        ({}, (WriteMode.NONE, None)),
        ({"output": True}, (WriteMode.COMPANION, None)),
        ({"output_dir": "out"}, (WriteMode.DIRECTORY, "out")),
        ({"in_place": True}, (WriteMode.IN_PLACE, None)),
        ({"in_place": True, "output_dir": "out", "output": True}, (WriteMode.IN_PLACE, None)),
        ({"output_dir": "out", "output": True}, (WriteMode.DIRECTORY, "out")),
    ])
    def test_in_place_beats_a_directory_beats_a_companion(self, flags, expected):
        assert cli._write_mode(_args(**flags)) == expected


class TestFormatSnapshotResult:
    def _result(self, tmp_path, isolated_env=None, **options):
        return endpoints.snapshot(_write_notebook(tmp_path), environment=ENV, **options)

    def test_unwritten_text_is_the_two_cells_to_paste_and_the_validation_report(self, tmp_path, isolated):
        result = self._result(tmp_path)
        cells, drift = result.notebooks[0].cells, result.notebooks[0].drift_report
        out = cli.format_snapshot_result(result)
        assert out.startswith("--- [ STEP 1: PASTE INTO CELL 1 (MARKDOWN) ] ---\n\n")
        assert cells.markdown in out and cells.code in out and "--- [ STEP 2: PASTE INTO CELL 2 (CODE) ] ---" in out
        assert out.endswith(spy.format_console_drift_report(drift))

    def test_written_text_is_only_the_validation_report(self, tmp_path, isolated):
        from steady_py.results import SnapshotOptions
        result = self._result(tmp_path, options=SnapshotOptions(write_mode=WriteMode.COMPANION))
        assert cli.format_snapshot_result(result) == spy.format_console_drift_report(result.notebooks[0].drift_report)

    def test_json_names_the_written_notebook_and_includes_the_validation_report(self, tmp_path, isolated):
        from steady_py.results import SnapshotOptions
        result = self._result(tmp_path, options=SnapshotOptions(write_mode=WriteMode.COMPANION))
        report = json.loads(cli.format_snapshot_result(result, "json"))
        assert report["artifacts_written"] == {"locked_notebook": result.notebooks[0].written_path}
        assert report["drift_check"]["manifest"]["dependencies"][0]["name"] == "requests"

    def test_unwritten_json_is_the_analysis_report_alone(self, tmp_path, isolated):
        result = self._result(tmp_path)
        assert cli.format_snapshot_result(result, "json") == spy.format_json_single_report(result.notebooks[0].report)

    def test_scan_json_is_the_analysis_report(self, tmp_path, isolated):
        scanned = endpoints.scan(_write_notebook(tmp_path), ENV)
        assert cli.format_scan_result(scanned) == spy.format_json_single_report(scanned.notebooks[0].report)


class TestRunSingleFile:
    def test_text_prints_the_cells_and_returns_0(self, tmp_path, isolated, capsys):
        assert cli.run_single_file(_args(notebook=_write_notebook(tmp_path))) == 0
        out = capsys.readouterr().out
        assert "STEP 1: PASTE INTO CELL 1" in out and "STEADY_PY_MANIFEST" in out

    def test_json_without_a_write_flag_is_a_scan_and_never_contacts_pypi(self, tmp_path, isolated, capsys, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("a scan must not check pins")
        monkeypatch.setattr(spy, "run_pin_checks", refuse)
        assert cli.run_single_file(_args(notebook=_write_notebook(tmp_path), format="json")) == 0
        report = json.loads(capsys.readouterr().out)
        assert [d["name"] for d in report["dependencies"]] == ["requests"]

    def test_output_writes_the_companion_and_says_so(self, tmp_path, isolated, capsys, caplog):
        path = _write_notebook(tmp_path)
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert cli.run_single_file(_args(notebook=path, output=True)) == 0
        assert (tmp_path / "nb_merged.ipynb").exists()
        assert "Writing updated notebook (suffix: '_merged')..." in caplog.text
        assert f"Updated '{tmp_path / 'nb_merged.ipynb'}'" in caplog.text
        assert "STEP 1" not in capsys.readouterr().out

    def test_output_dir_and_in_place_are_described_as_they_were(self, tmp_path, isolated, caplog):
        path = _write_notebook(tmp_path)
        with caplog.at_level(logging.INFO, logger="steady_py"):
            cli.run_single_file(_args(notebook=path, output_dir=str(tmp_path / "out"), suffix="_x"))
            cli.run_single_file(_args(notebook=path, in_place=True))
        assert f"directory: '{tmp_path / 'out'}', suffix: '_x'" in caplog.text
        assert "Writing updated notebook (in-place)..." in caplog.text

    def test_an_unreadable_notebook_logs_the_error_and_returns_1(self, tmp_path, isolated, capsys, caplog):
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert cli.run_single_file(_args(notebook=_unreadable(tmp_path))) == 1
        assert "❌ Error:" in caplog.text
        assert capsys.readouterr().out == ""

    def test_an_unreadable_notebook_in_json_mode_still_prints_a_report(self, tmp_path, isolated, capsys):
        assert cli.run_single_file(_args(notebook=_unreadable(tmp_path), format="json")) == 1
        assert json.loads(capsys.readouterr().out)["parse_error"]

    def test_no_target_outside_a_live_session_does_nothing(self, isolated, capsys, monkeypatch):
        monkeypatch.setattr(spy, "is_running_in_ipython", lambda: False)
        assert cli.run_single_file(_args()) == 0
        assert capsys.readouterr().out == ""

    def test_a_failed_write_logs_the_error_and_returns_1(self, tmp_path, isolated, caplog):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file", encoding="utf-8")
        with caplog.at_level(logging.INFO, logger="steady_py"):
            code = cli.run_single_file(_args(notebook=_write_notebook(tmp_path), output_dir=str(blocker / "out")))
        assert code == 1 and "could not write" in caplog.text

    def test_diagnostics_are_logged_in_text_mode_only(self, tmp_path, isolated, caplog):
        path = _write_notebook(tmp_path, 'import importlib\nname = "requests"\nimportlib.import_module(name)\nimport requests')
        with caplog.at_level(logging.INFO, logger="steady_py"):
            cli.run_single_file(_args(notebook=path, format="json"))
        assert "DIAGNOSTIC WARNINGS" not in caplog.text
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="steady_py"):
            cli.run_single_file(_args(notebook=path))
        assert "DIAGNOSTIC WARNINGS" in caplog.text and "Dynamic import detected" in caplog.text
