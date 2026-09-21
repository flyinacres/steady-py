"""The CLI layer: formatting and exit-code mapping are pure functions of a result; run_check ties
them to the endpoint and to stdout/stderr."""
import argparse
import json
import logging
import sys
from pathlib import Path

import pytest

import steady_py.cli as cli
import steady_py.core as spy
import steady_py.endpoints as endpoints
from steady_py.results import (
    CheckResult, Delta, Environment, PackageChange, NotebookCheck, NotebookScan, NotebookSnapshot, ScanResult, SetupCells,
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

    def test_drift_and_an_unreadable_manifest_together_is_1(self):
        drift = _checked(_finding(spy.Signal.YANKED, spy.Severity.CONFIRMED))
        result = _result(drift, NotebookCheck(path="b.ipynb", error="unreadable"))
        assert cli.check_exit_code(result) == 1

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

    def test_json_for_a_notebook_with_no_manifest_is_json_not_text(self):
        out, err = cli.format_check_result(_result(NotebookCheck(path="a.ipynb")), "json")
        payload = json.loads(out)
        assert (payload["mode"], payload["target"], payload["manifest_found"], payload["error"]) == ("check_drift", "a.ipynb", False, None)
        assert err == ""

    def test_json_for_an_unreadable_manifest_is_json_on_stdout_and_the_message_on_stderr(self):
        out, err = cli.format_check_result(_result(NotebookCheck(path="a.ipynb", error="boom")), "json")
        payload = json.loads(out)
        assert (payload["manifest_found"], payload["error"]) == (False, "boom")
        assert err == "⚠️ boom"


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
    """0 everything processed, 1 some of it, 2 nothing."""

    REPORT = spy.NotebookAnalysisReport(notebook_path="a.ipynb", is_python=True, lang_label="python")

    def _scan(self, *errors):
        return ScanResult(target="t", notebooks=[NotebookScan(path=f"n{i}", report=self.REPORT, error=e) for i, e in enumerate(errors)])

    def _snap(self, *errors, run_error=None):
        return SnapshotResult(target="t", error=run_error,
                              notebooks=[NotebookSnapshot(path=f"n{i}", report=self.REPORT, error=e) for i, e in enumerate(errors)])

    def test_everything_processed_is_0(self):
        assert cli.scan_exit_code(self._scan(None, None)) == 0
        assert cli.snapshot_exit_code(self._snap(None, None)) == 0

    def test_a_target_with_no_notebooks_is_0(self):
        assert cli.scan_exit_code(self._scan()) == 0 and cli.snapshot_exit_code(self._snap()) == 0

    def test_some_processed_and_some_not_is_1(self):
        assert cli.scan_exit_code(self._scan(None, "boom")) == 1
        assert cli.snapshot_exit_code(self._snap(None, "boom")) == 1

    def test_nothing_processed_is_2_including_a_single_unreadable_file(self):
        assert cli.scan_exit_code(self._scan("boom")) == 2
        assert cli.snapshot_exit_code(self._snap("boom", "boom")) == 2

    def test_a_failure_of_the_run_as_a_whole_counts_like_a_failed_notebook(self):
        assert cli.snapshot_exit_code(self._snap(None, run_error="could not write the universal manifest")) == 1
        assert cli.snapshot_exit_code(self._snap(run_error="could not write the universal manifest")) == 2


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

    def test_an_unreadable_notebook_logs_the_error_and_returns_2(self, tmp_path, isolated, capsys, caplog):
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert cli.run_single_file(_args(notebook=_unreadable(tmp_path))) == 2
        assert "❌ Error:" in caplog.text
        assert capsys.readouterr().out == ""

    def test_an_unreadable_notebook_in_json_mode_still_prints_a_report(self, tmp_path, isolated, capsys):
        assert cli.run_single_file(_args(notebook=_unreadable(tmp_path), format="json")) == 2
        assert json.loads(capsys.readouterr().out)["parse_error"]

    def test_no_target_outside_a_live_session_does_nothing(self, isolated, capsys, monkeypatch):
        monkeypatch.setattr(spy, "is_running_in_ipython", lambda: False)
        assert cli.run_single_file(_args()) == 0
        assert capsys.readouterr().out == ""

    def test_a_failed_write_logs_the_error_and_returns_2(self, tmp_path, isolated, caplog):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file", encoding="utf-8")
        with caplog.at_level(logging.INFO, logger="steady_py"):
            code = cli.run_single_file(_args(notebook=_write_notebook(tmp_path), output_dir=str(blocker / "out")))
        assert code == 2 and "could not write" in caplog.text

    def test_diagnostics_are_logged_in_text_mode_only(self, tmp_path, isolated, caplog):
        path = _write_notebook(tmp_path, 'import importlib\nname = "requests"\nimportlib.import_module(name)\nimport requests')
        with caplog.at_level(logging.INFO, logger="steady_py"):
            cli.run_single_file(_args(notebook=path, format="json"))
        assert "DIAGNOSTIC WARNINGS" not in caplog.text
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="steady_py"):
            cli.run_single_file(_args(notebook=path))
        assert "DIAGNOSTIC WARNINGS" in caplog.text and "Dynamic import detected" in caplog.text


class TestRunDirectory:
    """A directory of notebooks: scan when nothing is written, snapshot when something is."""

    @pytest.fixture(autouse=True)
    def offline(self, monkeypatch):
        monkeypatch.setattr(spy, "run_pin_checks", lambda deps, python_version: [])
        monkeypatch.setattr(spy, "inspect_gpu_environment", lambda imports: None)

    def _repo(self, tmp_path, corrupt=False):
        root = tmp_path / "repo"
        root.mkdir()
        _write_notebook(root, "import requests", "a.ipynb")
        _write_notebook(root, "import requests", "b.ipynb")
        if corrupt:
            (root / "bad.ipynb").write_text("{ not json", encoding="utf-8")
        return root

    def _run(self, root, **flags):
        return cli.run_directory(_args(batch=str(root), **flags), ENV)

    def test_without_a_write_flag_it_prints_the_analysis_and_never_contacts_pypi(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(spy, "run_pin_checks", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PyPI")))
        assert self._run(self._repo(tmp_path)) == 0
        assert "requests" in capsys.readouterr().out

    def test_json_without_a_write_flag_is_the_batch_report(self, tmp_path, capsys):
        assert self._run(self._repo(tmp_path), format="json") == 0
        report = json.loads(capsys.readouterr().out)
        assert report["mode"] == "batch" and report["summary"]["total_python_notebooks"] == 2
        assert not report["validation"] and not report["artifacts_written"]

    def test_the_positional_directory_works_like_batch(self, tmp_path, capsys):
        assert cli.run_directory(_args(notebook=str(self._repo(tmp_path))), ENV) == 0
        assert "requests" in capsys.readouterr().out

    def test_output_writes_companions_logs_each_and_prints_the_validation_section(self, tmp_path, capsys, caplog):
        root = self._repo(tmp_path)
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert self._run(root, output=True) == 0
        assert sorted(p.name for p in root.glob("*_merged.ipynb")) == ["a_merged.ipynb", "b_merged.ipynb"]
        assert "Writing per-notebook locked files (suffix: '_merged')..." in caplog.text
        assert f"Updated '{root / 'a_merged.ipynb'}'" in caplog.text and "Batch output complete." in caplog.text
        assert "STEADY_PY_MANIFEST" not in capsys.readouterr().out

    def test_json_output_lists_what_was_written_and_the_validation(self, tmp_path, capsys):
        root = self._repo(tmp_path)
        assert self._run(root, output=True, format="json") == 0
        report = json.loads(capsys.readouterr().out)
        assert sorted(Path(p).name for p in report["artifacts_written"]["locked_notebooks"]) == ["a_merged.ipynb", "b_merged.ipynb"]
        assert report["validation"]["notebooks_checked"] == 2

    def test_universal_writes_the_file_and_says_so(self, tmp_path, caplog):
        root = self._repo(tmp_path)
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert self._run(root, universal="all.txt") == 0
        assert (root / "all.txt").exists() and "Wrote universal repository manifest" in caplog.text
        assert not list(root.glob("*_merged.ipynb"))

    def test_an_unreadable_notebook_does_not_stop_the_rest_from_being_written(self, tmp_path, capsys, caplog):
        root = self._repo(tmp_path, corrupt=True)
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert self._run(root, output=True, universal="all.txt") == 1
        assert sorted(p.name for p in root.glob("*_merged.ipynb")) == ["a_merged.ipynb", "b_merged.ipynb"]
        assert (root / "all.txt").read_text(encoding="utf-8").startswith("# !!! INCOMPLETE: 1 notebook(s)")
        assert "1 notebook(s) could not be processed and were skipped" in caplog.text and "bad.ipynb" in caplog.text
        assert "Batch output complete." in caplog.text

    def test_json_still_reports_what_was_written_when_a_notebook_was_unreadable(self, tmp_path, capsys):
        root = self._repo(tmp_path, corrupt=True)
        assert self._run(root, output=True, format="json") == 1
        report = json.loads(capsys.readouterr().out)
        assert len(report["artifacts_written"]["locked_notebooks"]) == 2
        assert [Path(e["path"]).name for e in report["summary"]["parse_errors"]] == ["bad.ipynb"]

    def test_nothing_is_written_and_the_exit_is_2_when_nothing_could_be_read(self, tmp_path, caplog):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "bad.ipynb").write_text("{ not json", encoding="utf-8")
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert self._run(root, output=True, universal="all.txt") == 2
        assert sorted(p.name for p in root.iterdir()) == ["bad.ipynb"] and "bad.ipynb" in caplog.text

    def test_a_scan_with_an_unreadable_notebook_prints_the_report_lists_it_and_exits_1(self, tmp_path, capsys, caplog):
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert self._run(self._repo(tmp_path, corrupt=True)) == 1
        assert "bad.ipynb" in capsys.readouterr().out and "could not be processed and were skipped" in caplog.text

    def test_a_write_that_fails_is_named_and_the_rest_are_written(self, tmp_path, monkeypatch, caplog):
        root = self._repo(tmp_path)
        original = spy.write_locked_notebook

        def flaky(scan_res, *args, **kwargs):
            if scan_res.path.name == "a.ipynb":
                raise OSError("disk full")
            return original(scan_res, *args, **kwargs)
        monkeypatch.setattr(spy, "write_locked_notebook", flaky)
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert self._run(root, output=True) == 1
        assert [p.name for p in root.glob("*_merged.ipynb")] == ["b_merged.ipynb"]
        assert "a.ipynb: could not write the locked notebook: disk full" in caplog.text

    def test_a_universal_file_that_cannot_be_written_logs_the_error_and_returns_1(self, tmp_path, caplog):
        root = self._repo(tmp_path)
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert self._run(root, output=True, universal="missing_dir/all.txt") == 1
        assert "could not write the universal manifest" in caplog.text
        assert len(list(root.glob("*_merged.ipynb"))) == 2

    def test_a_directory_that_does_not_exist_is_an_error_not_an_empty_report(self, tmp_path, capsys, caplog):
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert self._run(tmp_path / "nowhere", output=True) == 2
        assert "is not a directory" in caplog.text and capsys.readouterr().out == ""


class TestMain:
    """main() parses the flags, dispatches to one verb, and exits with its code."""

    @pytest.fixture(autouse=True)
    def quiet_logger(self):
        level = spy.logger.level
        yield
        spy.logger.setLevel(level)

    def _main(self, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["steady-py", *argv])
        monkeypatch.setattr(spy, "is_running_in_ipython", lambda: False)
        cli.main()

    def _exit_code(self, monkeypatch, *argv):
        with pytest.raises(SystemExit) as excinfo:
            self._main(monkeypatch, *argv)
        return excinfo.value.code

    def test_a_file_goes_to_the_single_file_runner_and_its_code_is_the_exit_code(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(cli, "run_single_file", lambda args: calls.append(args.notebook) or 1)
        path = _write_notebook(tmp_path)
        assert self._exit_code(monkeypatch, path) == 1 and calls == [path]

    def test_a_clean_run_returns_without_exiting(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "run_single_file", lambda args: 0)
        self._main(monkeypatch, _write_notebook(tmp_path))

    @pytest.mark.parametrize("via_flag", [True, False])
    def test_a_directory_goes_to_the_directory_runner(self, tmp_path, monkeypatch, via_flag):
        seen = []
        monkeypatch.setattr(cli, "run_directory", lambda args: seen.append((args.batch, args.notebook)) or 0)
        monkeypatch.setattr(cli, "run_single_file", lambda args: pytest.fail("a directory is not a single file"))
        self._main(monkeypatch, *(["--batch", str(tmp_path)] if via_flag else [str(tmp_path)]))
        expected = [(str(tmp_path), None)] if via_flag else [(None, str(tmp_path))]
        assert seen == expected

    def test_check_drift_runs_check_with_the_format_and_root_dir(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(cli, "run_check", lambda target, output_format, root_dir: seen.append((target, output_format, root_dir)) or 1)
        path = _write_notebook(tmp_path)
        assert self._exit_code(monkeypatch, "--check-drift", path, "--format", "json", "--root-dir", "/repo") == 1
        assert seen == [(path, "json", "/repo")]

    def test_check_drift_needs_an_existing_file_or_directory(self, tmp_path, monkeypatch, caplog):
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert self._exit_code(monkeypatch, "--check-drift") == 2
            assert self._exit_code(monkeypatch, "--check-drift", str(tmp_path / "missing.ipynb")) == 2
        assert "requires a target notebook, .py file or directory" in caplog.text

    def test_check_drift_accepts_a_directory(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(cli, "run_check", lambda target, output_format, root_dir: seen.append(target) or 0)
        assert self._exit_code(monkeypatch, "--check-drift", str(tmp_path)) == 0
        assert seen == [str(tmp_path)]

    def test_write_flags_need_a_target(self, monkeypatch, caplog):
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert self._exit_code(monkeypatch, "--output") == 2
        assert "requires a target notebook file path or --batch directory" in caplog.text

    def test_no_target_outside_a_live_session_prints_usage_and_exits_2(self, monkeypatch, capsys, caplog):
        with caplog.at_level(logging.INFO, logger="steady_py"):
            assert self._exit_code(monkeypatch) == 2
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err.startswith("usage: steady-py")
        assert "a target notebook file, a directory, or --batch DIR is required" in caplog.text

    def test_a_live_session_returns_instead_of_exiting(self, monkeypatch, caplog):
        monkeypatch.setattr(sys, "argv", ["steady-py", "--output"])
        monkeypatch.setattr(spy, "is_running_in_ipython", lambda: True)
        with caplog.at_level(logging.INFO, logger="steady_py"):
            cli.main()
        assert "requires a target" in caplog.text

    @pytest.mark.parametrize("flag, level", [("--quiet", logging.ERROR), ("--verbose", logging.DEBUG)])
    def test_quiet_and_verbose_set_the_log_level(self, tmp_path, monkeypatch, flag, level):
        monkeypatch.setattr(cli, "run_single_file", lambda args: 0)
        self._main(monkeypatch, _write_notebook(tmp_path), flag)
        assert spy.logger.level == level


# ---------------------------------------------------------------------------------------------
# the delta

class TestFormatDelta:
    def test_no_changes_says_so(self):
        assert cli.format_delta(Delta(), "nb.ipynb") == "No changes since the existing manifest in nb.ipynb."

    def test_lists_added_removed_and_changed_packages(self):
        delta = Delta(
            added=[PackageChange("numpy", new_version="2.0.1")],
            removed=[PackageChange("scipy", old_version="1.11.0")],
            version_changes=[PackageChange("pandas", "2.1.0", "2.2.0")],
        )
        lines = cli.format_delta(delta, "nb.ipynb").splitlines()
        assert lines[0] == "Changes since the existing manifest in nb.ipynb:"
        assert lines[1:4] == ["  + numpy==2.0.1 (added)", "  - scipy==1.11.0 (removed)", "  ~ pandas 2.1.0 -> 2.2.0"]

    def test_version_changes_carry_the_environment_caveat_and_other_changes_do_not(self):
        with_versions = cli.format_delta(Delta(version_changes=[PackageChange("pandas", "2.1.0", "2.2.0")]), "nb.ipynb")
        assert "come from the environment this ran in" in with_versions
        assert "environment this ran in" not in cli.format_delta(Delta(added=[PackageChange("numpy", new_version="1")]), "nb.ipynb")

    def test_python_and_accelerator_changes(self):
        delta = Delta(python_version=("3.11", "3.12"), gpu=(None, {"has_gpu": True, "device_name": "A100"}))
        text = cli.format_delta(delta, "nb.ipynb")
        assert "  Python 3.11 -> 3.12" in text and "  Accelerator: none -> A100" in text

    def test_findings_are_listed_when_the_baseline_was_compared(self):
        delta = Delta(baseline_compared=True, findings_appeared=[("yanked", "requests", "2.32.3")], findings_resolved=[("stale", "numpy")])
        text = cli.format_delta(delta, "nb.ipynb")
        assert "  New findings: yanked:requests:2.32.3" in text and "  Resolved findings: stale:numpy" in text


class TestDeltaInOutput:
    """Where the delta shows up: only when the notebook already carried a manifest."""

    @pytest.fixture(autouse=True)
    def offline(self, monkeypatch):
        monkeypatch.setattr(spy, "run_pin_checks", lambda deps, python_version: [])
        monkeypatch.setattr(spy, "inspect_gpu_environment", lambda imports: None)

    NEWER = Environment(frozen_env={"requests": "requests==2.32.4"}, pkg_dist_map={"requests": ["requests"]})

    def _locked(self, tmp_path, name="nb.ipynb"):
        path = _write_notebook(tmp_path, name=name)
        endpoints.snapshot(path, __import__("steady_py.results", fromlist=["x"]).SnapshotOptions(write_mode=WriteMode.IN_PLACE), ENV)
        return path

    def test_unwritten_text_puts_the_delta_before_the_cells(self, tmp_path):
        path = self._locked(tmp_path)
        out = cli.format_snapshot_result(endpoints.snapshot(path, environment=self.NEWER))
        assert out.startswith("Changes since the existing manifest in ") and "~ requests 2.32.3 -> 2.32.4" in out
        assert out.index("Changes since") < out.index("STEP 1: PASTE INTO CELL 1")

    def test_written_text_puts_the_delta_before_the_validation_report(self, tmp_path):
        from steady_py.results import SnapshotOptions
        path = self._locked(tmp_path)
        result = endpoints.snapshot(path, SnapshotOptions(write_mode=WriteMode.COMPANION), self.NEWER)
        out = cli.format_snapshot_result(result)
        assert out.startswith("Changes since") and out.endswith(spy.format_console_drift_report(result.notebooks[0].drift_report))

    def test_a_notebook_without_a_manifest_prints_no_delta(self, tmp_path):
        path = _write_notebook(tmp_path)
        assert "Changes since" not in cli.format_snapshot_result(endpoints.snapshot(path, environment=ENV))

    def test_json_carries_the_delta_or_null(self, tmp_path):
        locked = self._locked(tmp_path)
        report = json.loads(cli.format_scan_result(endpoints.scan(locked, environment=self.NEWER)))
        assert report["delta"]["version_changes"] == [{"name": "requests", "old_version": "2.32.3", "new_version": "2.32.4"}]
        plain = json.loads(cli.format_scan_result(endpoints.scan(_write_notebook(tmp_path, name="plain.ipynb"), environment=ENV)))
        assert plain["delta"] is None

    def test_the_directory_report_lists_each_notebook_that_had_a_manifest(self, tmp_path, capsys, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        self._locked(root, "locked.ipynb")
        _write_notebook(root, name="plain.ipynb")
        assert cli.run_directory(_args(batch=str(root)), self.NEWER) == 0
        out = capsys.readouterr().out
        assert "Changes since the existing manifest in locked.ipynb:" in out and "plain.ipynb: " not in out.split("Changes since")[-1]

    def test_the_directory_json_keys_deltas_by_relative_path(self, tmp_path, capsys):
        root = tmp_path / "repo"
        root.mkdir()
        self._locked(root, "locked.ipynb")
        _write_notebook(root, name="plain.ipynb")
        cli.run_directory(_args(batch=str(root), format="json"), self.NEWER)
        report = json.loads(capsys.readouterr().out)
        assert list(report["deltas"]) == ["locked.ipynb"]
        assert report["deltas"]["locked.ipynb"]["version_changes"][0]["new_version"] == "2.32.4"

    def test_the_directory_json_has_null_deltas_when_no_notebook_had_a_manifest(self, tmp_path, capsys):
        root = tmp_path / "repo"
        root.mkdir()
        _write_notebook(root, name="plain.ipynb")
        cli.run_directory(_args(batch=str(root), format="json"), ENV)
        assert json.loads(capsys.readouterr().out)["deltas"] is None


# ---------------------------------------------------------------------------------------------
# check over a directory

def _dir_result(*notebooks):
    validation = spy.build_batch_validation([(Path(n.path).name, n.report) for n in notebooks if n.report is not None])
    return CheckResult(target="repo", kind=TargetKind.DIRECTORY, notebooks=list(notebooks),
                       validation=validation if any(n.report for n in notebooks) else None)


def _clean(name):
    return _checked(path=name)


def _drifted(name):
    return _checked(_finding(spy.Signal.YANKED, spy.Severity.CONFIRMED), path=name)


def _cannot_check(name):
    return _checked(_finding(spy.Signal.CHECK_ERROR, spy.Severity.ERROR), path=name)


class TestCheckExitCodeOverADirectory:
    def test_all_clean_is_0_and_no_manifest_is_no_problem(self):
        assert cli.check_exit_code(_dir_result(_clean("a"), NotebookCheck(path="plain"))) == 0

    def test_no_notebooks_is_0(self):
        assert cli.check_exit_code(CheckResult(target="repo", kind=TargetKind.DIRECTORY)) == 0

    def test_drift_in_any_notebook_is_1(self):
        assert cli.check_exit_code(_dir_result(_clean("a"), _drifted("b"))) == 1

    def test_some_notebooks_that_could_not_be_checked_among_others_that_were_is_1(self):
        assert cli.check_exit_code(_dir_result(_clean("a"), _cannot_check("b"))) == 1
        assert cli.check_exit_code(_dir_result(_clean("a"), NotebookCheck(path="b", error="unreadable"))) == 1

    def test_when_nothing_could_be_checked_it_is_2(self):
        assert cli.check_exit_code(_dir_result(_cannot_check("a"), NotebookCheck(path="b", error="unreadable"))) == 2
        assert cli.check_exit_code(_dir_result(NotebookCheck(path="b", error="unreadable"))) == 2

    def test_a_notebook_with_no_manifest_does_not_count_as_checked_or_failed(self):
        assert cli.check_exit_code(_dir_result(NotebookCheck(path="plain"), NotebookCheck(path="b", error="unreadable"))) == 1


class TestFormatCheckDirectory:
    def test_text_summarizes_then_shows_the_aggregate_validation(self):
        result = _dir_result(_drifted("a.ipynb"), _clean("b.ipynb"), NotebookCheck(path="plain.ipynb"))
        out, err = cli.format_check_result(result)
        assert out.startswith("Checked 3 notebook(s) in repo: 2 with a manifest, 1 without (nothing to check), 0 could not be read.")
        assert out.endswith(spy.format_console_batch_validation(result.validation)) and err == ""

    def test_unreadable_manifests_are_named_on_stderr(self):
        result = _dir_result(_clean("a.ipynb"), NotebookCheck(path="repo/bad.ipynb", error="boom"))
        out, err = cli.format_check_result(result)
        assert "1 could not be read" in out and err == "⚠️ bad.ipynb: boom"

    def test_an_empty_directory_says_so(self):
        out, _ = cli.format_check_result(CheckResult(target="repo", kind=TargetKind.DIRECTORY))
        assert out == "No notebooks found in repo -- nothing to check."

    def test_json_lists_every_notebook_and_the_validation(self):
        result = _dir_result(_drifted("a.ipynb"), NotebookCheck(path="plain.ipynb"), NotebookCheck(path="bad.ipynb", error="boom"))
        payload = json.loads(cli.format_check_result(result, "json")[0])
        assert (payload["mode"], payload["target_dir"]) == ("check_batch", "repo")
        assert payload["summary"] == {"notebooks": 3, "with_manifest": 1, "without_manifest": 1, "unreadable": 1}
        by_path = {n["path"]: n for n in payload["notebooks"]}
        assert by_path["a.ipynb"]["drift_check"]["target"] == "a.ipynb" and by_path["a.ipynb"]["manifest_found"] is True
        assert by_path["plain.ipynb"]["manifest_found"] is False and by_path["bad.ipynb"]["error"] == "boom"
        assert payload["validation"]["notebooks_checked"] == 1


class TestRunCheckDirectory:
    def test_prints_the_summary_and_returns_the_worst_case_rule(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(spy, "run_pin_checks", lambda deps, python_version: [])
        _write_notebook(tmp_path, spy.generate_production_blueprint([PIN])["step2_code"], "a.ipynb")
        _write_notebook(tmp_path, "import requests", "plain.ipynb")
        assert cli.run_check(str(tmp_path)) == 0
        out = capsys.readouterr().out
        assert "Checked 2 notebook(s)" in out and "1 with a manifest, 1 without" in out
