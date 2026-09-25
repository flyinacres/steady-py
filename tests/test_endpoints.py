"""The endpoints compute typed results; they never print or exit. check is the first."""
import json
from pathlib import Path

import pytest

import steady_py.core as spy
from steady_py import accelerator, analyze, constants, drift, installed, localmodules, models, scanning, util
from steady_py.endpoints import check, scan, snapshot
from steady_py.results import CheckOptions, Environment, PackageChange, ScanOptions, SnapshotOptions, TargetKind, WriteMode

DEPS = [models.PinnedDependency("requests", "2.32.1")]


@pytest.fixture
def offline(monkeypatch):
    """No PyPI: pin checks return nothing unless a test says otherwise."""
    monkeypatch.setattr(drift, "run_pin_checks", lambda deps, python_version: [])


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
        assert [f.signal for f in report.confirmed] == [constants.Signal.TAMPERED]

    def test_pin_checks_run_on_the_manifests_pins_and_python(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(drift, "run_pin_checks", lambda deps, python_version: seen.append((deps, python_version)) or [])
        path = _notebook_with_manifest(tmp_path)
        check(str(path))
        (deps, python_version), = seen[-1:]
        assert deps == DEPS
        assert python_version == {"major": spy.sys.version_info.major, "minor": spy.sys.version_info.minor}

    def test_findings_from_the_pin_checks_land_in_the_report(self, tmp_path, monkeypatch):
        finding = models.DriftFinding("requests", "2.32.1", constants.Signal.YANKED, constants.Severity.CONFIRMED, "yanked")
        path = _notebook_with_manifest(tmp_path)
        monkeypatch.setattr(drift, "run_pin_checks", lambda deps, python_version: [finding])
        assert check(str(path)).notebooks[0].report.confirmed == [finding]

    def test_root_dir_and_notebook_dir_reach_the_local_module_check(self, tmp_path, monkeypatch, offline):
        seen = {}
        monkeypatch.setattr(localmodules, "check_local_modules", lambda manifest, notebook_dir, root_dir=None: seen.update(nb=notebook_dir, root=root_dir) or [])
        path = _notebook_with_manifest(tmp_path)
        check(str(path), CheckOptions(root_dir="/repo"))
        assert seen == {"nb": str(tmp_path), "root": "/repo"}

    def test_computes_without_printing(self, tmp_path, offline, capsys):
        check(str(_notebook_with_manifest(tmp_path)))
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""


class TestCheckDirectory:
    """check over a directory: one result per notebook, plus the aggregate validation section."""

    def _tree(self, tmp_path, with_manifest=("a.ipynb", "sub/b.ipynb")):
        root = tmp_path / "repo"
        (root / "sub").mkdir(parents=True)
        for rel in ("a.ipynb", "sub/b.ipynb", "plain.ipynb"):
            target = root / rel
            if rel in with_manifest:
                _notebook(target.parent, spy.generate_production_blueprint(DEPS)["step2_code"], target.name)
            else:
                _notebook(target.parent, "import requests", target.name)
        return str(root)

    def test_returns_one_result_per_notebook_and_an_aggregate(self, tmp_path, offline):
        root = self._tree(tmp_path)
        result = check(root)
        assert (result.target, result.kind) == (root, TargetKind.DIRECTORY)
        by_name = {Path(n.path).name: n for n in result.notebooks}
        assert sorted(by_name) == ["a.ipynb", "b.ipynb", "plain.ipynb"]
        assert by_name["a.ipynb"].manifest_found and by_name["b.ipynb"].manifest_found
        assert not by_name["plain.ipynb"].manifest_found and by_name["plain.ipynb"].error is None
        assert result.validation.notebooks_checked == 2

    def test_generated_companion_files_are_checked_too(self, tmp_path, offline):
        root = self._tree(tmp_path, with_manifest=())
        _notebook(tmp_path / "repo", spy.generate_production_blueprint(DEPS)["step2_code"], "plain_merged.ipynb")
        found = {Path(n.path).name: n.manifest_found for n in check(root).notebooks}
        assert found["plain_merged.ipynb"] is True

    def test_hidden_and_ignored_directories_are_skipped(self, tmp_path, offline):
        root = self._tree(tmp_path)
        for skipped in (".hidden", ".ipynb_checkpoints", "build", "venv"):
            (Path(root) / skipped).mkdir()
            _notebook(Path(root) / skipped, "import requests", "x.ipynb")
        assert sorted(Path(n.path).name for n in check(root).notebooks) == ["a.ipynb", "b.ipynb", "plain.ipynb"]

    def test_an_unreadable_manifest_is_that_notebooks_error_and_the_rest_are_still_checked(self, tmp_path, offline):
        root = self._tree(tmp_path)
        _notebook(Path(root), "STEADY_PY_MANIFEST = {'python_version': 3}", "broken.ipynb")
        result = check(root)
        assert [Path(n.path).name for n in result.failed] == ["broken.ipynb"]
        assert result.validation.notebooks_checked == 2

    def test_a_finding_shared_by_notebooks_is_grouped_once(self, tmp_path, monkeypatch):
        finding = models.DriftFinding("requests", "2.32.1", constants.Signal.YANKED, constants.Severity.CONFIRMED, "yanked")
        monkeypatch.setattr(drift, "run_pin_checks", lambda deps, python_version: [finding])
        root = self._tree(tmp_path)
        group, = check(root).validation.findings
        assert sorted(group.notebooks) == ["a.ipynb", "sub/b.ipynb"]

    def test_the_directory_is_the_default_root_dir_and_an_explicit_one_wins(self, tmp_path, monkeypatch, offline):
        seen = []
        monkeypatch.setattr(localmodules, "check_local_modules", lambda manifest, notebook_dir, root_dir=None: seen.append(root_dir) or [])
        root = self._tree(tmp_path)
        check(root)
        assert set(seen) == {root}
        seen.clear()
        check(root, CheckOptions(root_dir="/elsewhere"))
        assert set(seen) == {"/elsewhere"}

    def test_an_empty_directory_has_nothing_to_check(self, tmp_path, offline):
        result = check(str(tmp_path))
        assert (result.notebooks, result.validation) == ([], None)


# ---------------------------------------------------------------------------------------------
# scan and snapshot

ENV = Environment(
    frozen_env={"requests": "requests==2.32.3", "torch": "torch==2.1.0+cu118"},
    pkg_dist_map={"requests": ["requests"], "torch": ["torch"]},
    raw_full_freeze=["requests==2.32.3", "torch==2.1.0+cu118"],
)


@pytest.fixture
def isolated(monkeypatch):
    """No PyPI, no accelerator probing, and no detection of the real environment."""
    monkeypatch.setattr(drift, "run_pin_checks", lambda deps, python_version: [])
    monkeypatch.setattr(accelerator, "inspect_gpu_environment", lambda imports: None)

    def no_detection():
        raise AssertionError("the environment should have been passed in")
    monkeypatch.setattr(installed, "get_installed_environment", no_detection)


def _source(tmp_path, source="import requests", name="nb.ipynb"):
    return str(_notebook(tmp_path, source, name))


def _cells_text(path):
    cells = json.loads(path.read_text(encoding="utf-8"))["cells"]
    return ["".join(c["source"]) for c in cells]


class TestScan:
    def test_reports_what_the_notebook_needs(self, tmp_path, isolated):
        result = scan(_source(tmp_path), environment=ENV)
        notebook, = result.notebooks
        assert (result.kind, notebook.error) == (TargetKind.FILE, None)
        assert [d.name for d in notebook.report.dependencies] == ["requests"]

    def test_never_contacts_pypi(self, tmp_path, isolated, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("scan must not check pins")
        monkeypatch.setattr(drift, "run_pin_checks", refuse)
        monkeypatch.setattr(spy, "generate_production_blueprint", refuse)
        scan(_source(tmp_path), environment=ENV)

    def test_an_unreadable_file_is_an_error_with_a_report_carrying_it(self, tmp_path, isolated):
        bad = tmp_path / "bad.ipynb"
        bad.write_text("{ not json", encoding="utf-8")
        notebook, = scan(str(bad), environment=ENV).notebooks
        assert notebook.error and notebook.report.parse_error == notebook.error
        assert notebook.report.is_python is False

    def test_detects_the_environment_when_none_is_given(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accelerator, "inspect_gpu_environment", lambda imports: None)
        monkeypatch.setattr(installed, "get_installed_environment", lambda: ({"requests": "requests==2.32.3"}, []))
        notebook, = scan(_source(tmp_path)).notebooks
        assert [d.version for d in notebook.report.dependencies] == ["2.32.3"]

    def test_the_live_session_is_a_target_when_none_is_given(self, isolated, monkeypatch):
        monkeypatch.setattr(util, "is_running_in_ipython", lambda: True)
        monkeypatch.setattr(scanning, "extract_from_active_session", lambda: (["requests"], {}, ["import requests"], set(), []))
        result = scan(None, environment=ENV)
        assert (result.kind, result.notebooks[0].path) == (TargetKind.SESSION, "session.ipynb")
        assert [d.name for d in result.notebooks[0].report.dependencies] == ["requests"]

    def test_no_target_outside_a_live_session_is_a_usage_error(self, isolated, monkeypatch):
        monkeypatch.setattr(util, "is_running_in_ipython", lambda: False)
        with pytest.raises(ValueError, match="target"):
            scan(None, environment=ENV)


class TestSnapshot:
    def test_default_returns_the_cells_and_writes_nothing(self, tmp_path, isolated):
        path = _source(tmp_path)
        before = sorted(p.name for p in tmp_path.iterdir())
        notebook, = snapshot(path, environment=ENV).notebooks
        assert notebook.cells.markdown and "STEADY_PY_MANIFEST" in notebook.cells.code
        assert [d.name for d in notebook.manifest.dependencies] == ["requests"]
        assert (notebook.written_path, notebook.error) == (None, None)
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_companion_file_holds_the_same_cells_the_result_returned(self, tmp_path, isolated):
        path = _source(tmp_path)
        notebook, = snapshot(path, SnapshotOptions(write_mode=WriteMode.COMPANION), ENV).notebooks
        written = tmp_path / "nb_merged.ipynb"
        assert notebook.written_path == str(written)
        assert [c.rstrip("\n") for c in _cells_text(written)[:2]] == [notebook.cells.markdown.rstrip("\n"), notebook.cells.code.rstrip("\n")]
        assert "import requests" in _cells_text(written)[2]

    def test_directory_mode_writes_into_the_directory_with_the_suffix(self, tmp_path, isolated):
        options = SnapshotOptions(write_mode=WriteMode.DIRECTORY, output_dir=str(tmp_path / "out"), suffix="_x")
        notebook, = snapshot(_source(tmp_path), options, ENV).notebooks
        assert notebook.written_path == str(tmp_path / "out" / "nb_x.ipynb")

    def test_in_place_replaces_setup_cells_and_is_idempotent(self, tmp_path, isolated):
        path = _source(tmp_path)
        options = SnapshotOptions(write_mode=WriteMode.IN_PLACE)
        snapshot(path, options, ENV)
        first = _cells_text(tmp_path / "nb.ipynb")
        snapshot(path, options, ENV)
        assert len(_cells_text(tmp_path / "nb.ipynb")) == len(first) == 3

    def test_the_analysis_runs_once_however_the_result_is_delivered(self, tmp_path, isolated, monkeypatch):
        calls = []
        original = analyze.build_single_notebook_report
        monkeypatch.setattr(analyze, "build_single_notebook_report", lambda *a, **k: calls.append(1) or original(*a, **k))
        snapshot(_source(tmp_path), SnapshotOptions(write_mode=WriteMode.COMPANION), ENV)
        assert len(calls) == 1

    @pytest.mark.parametrize("mode", [WriteMode.NONE, WriteMode.COMPANION])
    def test_full_freeze_reaches_the_cells_whether_or_not_they_are_written(self, tmp_path, isolated, mode):
        notebook, = snapshot(_source(tmp_path), SnapshotOptions(write_mode=mode, full_freeze=True), ENV).notebooks
        assert "requests==2.32.3" in notebook.cells.code
        without, = snapshot(_source(tmp_path), SnapshotOptions(write_mode=mode), ENV).notebooks
        assert "FULL_FREEZE_FALLBACK" not in without.cells.code

    @pytest.mark.parametrize("mode", [WriteMode.NONE, WriteMode.COMPANION])
    def test_specific_builds_are_called_out_whether_or_not_the_cells_are_written(self, tmp_path, isolated, mode):
        path = _source(tmp_path, "import torch")
        notebook, = snapshot(path, SnapshotOptions(write_mode=mode), ENV).notebooks
        assert "Specific Package Builds Detected" in notebook.cells.markdown

    def test_the_install_timeout_is_baked_into_the_cells(self, tmp_path, isolated):
        path = _source(tmp_path)
        slow, = snapshot(path, SnapshotOptions(install_timeout=999), ENV).notebooks
        fast, = snapshot(path, SnapshotOptions(install_timeout=5), ENV).notebooks
        assert "999" in slow.cells.code and "999" not in fast.cells.code

    def test_an_unreadable_file_is_an_error_and_produces_no_cells(self, tmp_path, isolated):
        bad = tmp_path / "bad.ipynb"
        bad.write_text("{ not json", encoding="utf-8")
        notebook, = snapshot(str(bad), SnapshotOptions(write_mode=WriteMode.COMPANION), ENV).notebooks
        assert notebook.error and notebook.cells is None and notebook.written_path is None
        assert not (tmp_path / "bad_merged.ipynb").exists()

    def test_a_failed_write_is_an_error_but_the_cells_are_still_returned(self, tmp_path, isolated):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a directory", encoding="utf-8")
        options = SnapshotOptions(write_mode=WriteMode.DIRECTORY, output_dir=str(blocker / "out"))
        notebook, = snapshot(_source(tmp_path), options, ENV).notebooks
        assert "could not write" in notebook.error and notebook.written_path is None
        assert notebook.cells is not None

    def test_the_live_session_cannot_be_written_to_a_file(self, isolated):
        with pytest.raises(ValueError, match="session"):
            snapshot(None, SnapshotOptions(write_mode=WriteMode.COMPANION), ENV)

    def test_computes_without_printing(self, tmp_path, isolated, capsys):
        snapshot(_source(tmp_path), environment=ENV)
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""


# ---------------------------------------------------------------------------------------------
# directories

def _repo(tmp_path, corrupt=()):
    """A directory with a.ipynb and sub/b.ipynb, each importing requests, plus any corrupt notebooks."""
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    _notebook(root, "import requests", "a.ipynb")
    _notebook(root / "sub", "import requests", "b.ipynb")
    for name in corrupt:
        (root / name).write_text("{ not json", encoding="utf-8")
    return str(root)


class TestDirectoryScan:
    def test_returns_a_result_per_notebook_and_the_aggregate(self, tmp_path, isolated):
        result = scan(_repo(tmp_path), environment=ENV)
        assert result.kind == TargetKind.DIRECTORY
        assert sorted(Path(n.path).relative_to(tmp_path / "repo").as_posix() for n in result.notebooks) == ["a.ipynb", "sub/b.ipynb"]
        assert result.batch_summary.total_python_notebooks == 2 and result.failed == []
        assert "requests" in result.batch_summary.matched_packages

    def test_an_unreadable_notebook_is_a_failed_entry_and_the_rest_are_still_scanned(self, tmp_path, isolated):
        result = scan(_repo(tmp_path, corrupt=["bad.ipynb"]), environment=ENV)
        assert [Path(n.path).name for n in result.failed] == ["bad.ipynb"]
        assert result.failed[0].error and result.failed[0].report.parse_error == result.failed[0].error
        assert len(result.notebooks) == 3
        assert [Path(e["path"]).name for e in result.batch_summary.parse_errors] == ["bad.ipynb"]

    def test_generated_companion_files_are_skipped_by_suffix(self, tmp_path, isolated):
        root = _repo(tmp_path)
        _notebook(tmp_path / "repo", "import requests", "a_merged.ipynb")
        assert len(scan(root, environment=ENV).notebooks) == 2
        assert len(scan(root, ScanOptions(suffix="_other"), ENV).notebooks) == 3

    def test_never_contacts_pypi(self, tmp_path, isolated, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("scan must not check pins")
        monkeypatch.setattr(drift, "run_pin_checks", refuse)
        scan(_repo(tmp_path), environment=ENV)


class TestDirectorySnapshot:
    def test_default_returns_cells_for_every_notebook_and_writes_nothing(self, tmp_path, isolated):
        root = _repo(tmp_path)
        result = snapshot(root, environment=ENV)
        assert [n.cells is not None and n.written_path is None for n in result.notebooks] == [True, True]
        assert result.validation is None and result.universal_path is None
        assert sorted(p.name for p in (tmp_path / "repo").rglob("*_merged.ipynb")) == []

    def test_writes_a_companion_per_notebook_and_an_aggregate_validation(self, tmp_path, isolated):
        root = _repo(tmp_path)
        result = snapshot(root, SnapshotOptions(write_mode=WriteMode.COMPANION), ENV)
        assert sorted(Path(n.written_path).name for n in result.notebooks) == ["a_merged.ipynb", "b_merged.ipynb"]
        assert result.validation.notebooks_checked == 2

    def test_universal_manifest_is_written_into_the_directory(self, tmp_path, isolated):
        root = _repo(tmp_path)
        result = snapshot(root, SnapshotOptions(universal="all.txt"), ENV)
        assert result.universal_path == str(tmp_path / "repo" / "all.txt")
        assert "requests==2.32.3" in Path(result.universal_path).read_text(encoding="utf-8")

    def test_directory_mode_keeps_the_relative_layout(self, tmp_path, isolated):
        root = _repo(tmp_path)
        options = SnapshotOptions(write_mode=WriteMode.DIRECTORY, output_dir=str(tmp_path / "out"))
        snapshot(root, options, ENV)
        assert sorted(p.relative_to(tmp_path / "out").as_posix() for p in (tmp_path / "out").rglob("*.ipynb")) == ["a.ipynb", "sub/b.ipynb"]

    def test_in_place_does_not_skip_companion_named_files(self, tmp_path, isolated):
        root = _repo(tmp_path)
        _notebook(tmp_path / "repo", "import requests", "x_merged.ipynb")
        assert len(snapshot(root, SnapshotOptions(write_mode=WriteMode.IN_PLACE), ENV).notebooks) == 3

    def test_an_unreadable_notebook_does_not_stop_the_rest_from_being_written(self, tmp_path, isolated):
        root = _repo(tmp_path, corrupt=["bad.ipynb"])
        options = SnapshotOptions(write_mode=WriteMode.COMPANION, universal="all.txt")
        result = snapshot(root, options, ENV)
        assert [Path(n.path).name for n in result.failed] == ["bad.ipynb"]
        assert sorted(Path(n.written_path).name for n in result.notebooks if n.written_path) == ["a_merged.ipynb", "b_merged.ipynb"]
        assert result.validation.notebooks_checked == 2
        universal = Path(result.universal_path).read_text(encoding="utf-8").splitlines()
        assert universal[0].startswith("# !!! INCOMPLETE: 1 notebook(s)") and "bad.ipynb" in universal[1]
        assert "requests==2.32.3" in "\n".join(universal)

    def test_the_universal_file_is_not_marked_incomplete_when_everything_was_read(self, tmp_path, isolated):
        result = snapshot(_repo(tmp_path), SnapshotOptions(universal="all.txt"), ENV)
        assert "INCOMPLETE" not in Path(result.universal_path).read_text(encoding="utf-8")

    def test_nothing_is_written_when_nothing_could_be_read(self, tmp_path, isolated):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "bad.ipynb").write_text("{ not json", encoding="utf-8")
        result = snapshot(str(root), SnapshotOptions(write_mode=WriteMode.COMPANION, universal="all.txt"), ENV)
        assert len(result.failed) == 1 and result.notebooks == result.failed
        assert result.universal_path is None and result.validation is None
        assert sorted(p.name for p in root.iterdir()) == ["bad.ipynb"]

    def test_a_write_that_fails_costs_only_that_notebook(self, tmp_path, isolated, monkeypatch):
        root = _repo(tmp_path)
        original = spy.write_locked_notebook

        def flaky(scan_res, *args, **kwargs):
            if scan_res.path.name == "a.ipynb":
                raise OSError("disk full")
            return original(scan_res, *args, **kwargs)
        monkeypatch.setattr(spy, "write_locked_notebook", flaky)
        result = snapshot(root, SnapshotOptions(write_mode=WriteMode.COMPANION), ENV)
        assert [(Path(n.path).name, "disk full" in n.error) for n in result.failed] == [("a.ipynb", True)]
        assert [Path(n.written_path).name for n in result.notebooks if n.written_path] == ["b_merged.ipynb"]
        assert result.validation.notebooks_checked == 1

    def test_an_unreadable_notebook_does_not_stop_a_run_that_writes_nothing(self, tmp_path, isolated):
        result = snapshot(_repo(tmp_path, corrupt=["bad.ipynb"]), environment=ENV)
        assert len([n for n in result.notebooks if n.cells is not None]) == 2 and len(result.failed) == 1

    def test_a_universal_file_that_cannot_be_written_is_a_run_level_error_and_the_notebooks_are_still_written(self, tmp_path, isolated):
        root = _repo(tmp_path)
        options = SnapshotOptions(write_mode=WriteMode.COMPANION, universal="missing_dir/all.txt")
        result = snapshot(root, options, ENV)
        assert "universal" in result.error and result.universal_path is None
        assert len([n for n in result.notebooks if n.written_path]) == 2

    def test_universal_is_only_for_directories(self, tmp_path, isolated):
        with pytest.raises(ValueError, match="universal"):
            snapshot(_source(tmp_path), SnapshotOptions(universal="all.txt"), ENV)


# ---------------------------------------------------------------------------------------------
# the delta: an existing manifest against what a snapshot would produce now

ENV_NEWER = Environment(
    frozen_env={"requests": "requests==2.32.4", "torch": "torch==2.1.0+cu118"},
    pkg_dist_map={"requests": ["requests"], "torch": ["torch"]},
    raw_full_freeze=["requests==2.32.4"],
)


def _locked_notebook(tmp_path, name="nb.ipynb", source="import requests", environment=ENV):
    """A notebook that already carries a manifest, made by snapshotting it in place."""
    path = _source(tmp_path, source, name)
    snapshot(path, SnapshotOptions(write_mode=WriteMode.IN_PLACE), environment)
    return path


class TestScanDelta:
    def test_a_notebook_without_a_manifest_has_none(self, tmp_path, isolated):
        notebook, = scan(_source(tmp_path), environment=ENV).notebooks
        assert (notebook.manifest, notebook.manifest_error, notebook.delta) == (None, None, None)

    def test_an_unchanged_notebook_reports_no_changes(self, tmp_path, isolated):
        path = _locked_notebook(tmp_path)
        notebook, = scan(path, environment=ENV).notebooks
        assert [d.name for d in notebook.manifest.dependencies] == ["requests"]
        assert notebook.delta is not None and not notebook.delta.has_changes

    def test_a_different_environment_shows_version_changes_apart_from_added_and_removed(self, tmp_path, isolated):
        path = _locked_notebook(tmp_path)
        notebook, = scan(path, environment=ENV_NEWER).notebooks
        assert notebook.delta.version_changes == [PackageChange("requests", "2.32.3", "2.32.4")]
        assert notebook.delta.added == [] and notebook.delta.removed == []

    def test_a_changed_notebook_shows_added_and_removed_packages(self, tmp_path, isolated):
        path = _locked_notebook(tmp_path)
        edited = json.loads(open(path, encoding="utf-8").read())
        edited["cells"][-1]["source"] = ["import torch"]      # the notebook's own cell, after the two setup cells
        open(path, "w", encoding="utf-8").write(json.dumps(edited))
        delta = scan(path, environment=ENV).notebooks[0].delta
        assert [c.name for c in delta.added] == ["torch"] and [c.name for c in delta.removed] == ["requests"]

    def test_it_never_compares_the_baseline_because_it_never_contacts_pypi(self, tmp_path, isolated):
        assert scan(_locked_notebook(tmp_path), environment=ENV).notebooks[0].delta.baseline_compared is False

    def test_an_unreadable_manifest_is_reported_without_failing_the_scan(self, tmp_path, isolated):
        path = _source(tmp_path, "STEADY_PY_MANIFEST = {'python_version': 3}\nimport requests")
        notebook, = scan(path, environment=ENV).notebooks
        assert notebook.error is None and notebook.manifest is None and notebook.delta is None
        assert notebook.manifest_error

    def test_a_directory_scan_gives_each_notebook_its_own_delta(self, tmp_path, isolated):
        root = tmp_path / "repo"
        root.mkdir()
        _locked_notebook(root, "locked.ipynb")
        _source(root, "import requests", "plain.ipynb")
        deltas = {Path(n.path).name: n.delta for n in scan(str(root), environment=ENV).notebooks}
        assert deltas["plain.ipynb"] is None and deltas["locked.ipynb"] is not None

    def test_the_live_session_has_no_delta(self, isolated, monkeypatch):
        monkeypatch.setattr(util, "is_running_in_ipython", lambda: True)
        monkeypatch.setattr(scanning, "extract_from_active_session", lambda: (["requests"], {}, ["import requests"], set(), []))
        assert scan(None, environment=ENV).notebooks[0].delta is None


class TestSnapshotDelta:
    def test_a_notebook_without_a_manifest_has_none(self, tmp_path, isolated):
        assert snapshot(_source(tmp_path), environment=ENV).notebooks[0].delta is None

    def test_it_compares_the_baseline_because_it_validates_against_pypi(self, tmp_path, isolated):
        delta = snapshot(_locked_notebook(tmp_path), environment=ENV).notebooks[0].delta
        assert delta.baseline_compared is True and not delta.has_changes

    def test_a_newer_environment_shows_what_replacing_the_manifest_changes(self, tmp_path, isolated):
        path = _locked_notebook(tmp_path)
        delta = snapshot(path, environment=ENV_NEWER).notebooks[0].delta
        assert delta.version_changes == [PackageChange("requests", "2.32.3", "2.32.4")]

    def test_an_in_place_snapshot_compares_against_the_manifest_it_is_about_to_replace(self, tmp_path, isolated):
        path = _locked_notebook(tmp_path)
        options = SnapshotOptions(write_mode=WriteMode.IN_PLACE)
        first = snapshot(path, options, ENV_NEWER).notebooks[0]
        assert first.delta.version_changes == [PackageChange("requests", "2.32.3", "2.32.4")]
        second = snapshot(path, options, ENV_NEWER).notebooks[0]
        assert not second.delta.has_changes   # the first run already replaced it

    def test_a_companion_snapshot_leaves_the_source_manifest_in_place(self, tmp_path, isolated):
        path = _locked_notebook(tmp_path)
        delta = snapshot(path, SnapshotOptions(write_mode=WriteMode.COMPANION), ENV_NEWER).notebooks[0].delta
        assert delta.version_changes and scan(path, environment=ENV).notebooks[0].delta.has_changes is False

    def test_findings_that_appear_are_reported(self, tmp_path, isolated, monkeypatch):
        path = _locked_notebook(tmp_path)
        yanked = models.DriftFinding("requests", "2.32.3", constants.Signal.YANKED, constants.Severity.CONFIRMED, "yanked")
        monkeypatch.setattr(drift, "run_pin_checks", lambda deps, python_version: [yanked])
        delta = snapshot(path, environment=ENV).notebooks[0].delta
        assert delta.findings_appeared == [("yanked", "requests", "2.32.3")]

    def test_a_directory_snapshot_gives_each_notebook_its_own_delta(self, tmp_path, isolated):
        root = tmp_path / "repo"
        root.mkdir()
        _locked_notebook(root, "locked.ipynb")
        _source(root, "import requests", "plain.ipynb")
        deltas = {Path(n.path).name: n.delta for n in snapshot(str(root), environment=ENV).notebooks}
        assert deltas["plain.ipynb"] is None and deltas["locked.ipynb"] is not None
