"""The option and result types are plain data. What needs pinning is the little logic they carry
(option validation, `failed`, `has_changes`, `manifest`) and that importing them stays light."""
import dataclasses

import pytest

import steady_py
import steady_py.core as spy
from steady_py import constants, models
from steady_py.results import (
    CheckOptions, CheckResult, Delta, Environment, NotebookCheck, NotebookScan, NotebookSnapshot,
    PackageChange, ScanResult, SnapshotOptions, SnapshotResult, TargetKind, WriteMode,
)



def _report(path="a.ipynb"):
    return models.NotebookAnalysisReport(notebook_path=path, is_python=True, lang_label=constants.StatusLabel.PYTHON)


def _drift_report():
    manifest = models.SteadyPyManifest(python_version={"major": 3, "minor": 12}, dependencies=[], gpu=None, generated_at="t")
    return spy.build_drift_check_report("a.ipynb", manifest, [], kind=constants.ReportKind.VALIDATION)


class TestSnapshotOptions:
    def test_defaults_return_cells_and_write_nothing(self):
        opts = SnapshotOptions()
        assert opts.write_mode == WriteMode.NONE
        assert opts.install_timeout == 120
        assert opts.output_dir is None and opts.universal is None and not opts.full_freeze

    def test_is_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            SnapshotOptions().write_mode = WriteMode.IN_PLACE  # type: ignore[misc]

    def test_unknown_write_mode_is_rejected(self):
        with pytest.raises(ValueError, match="write_mode"):
            SnapshotOptions(write_mode="sideways")

    def test_directory_mode_needs_output_dir(self):
        with pytest.raises(ValueError, match="output_dir"):
            SnapshotOptions(write_mode=WriteMode.DIRECTORY)
        assert SnapshotOptions(write_mode=WriteMode.DIRECTORY, output_dir="out").output_dir == "out"

    @pytest.mark.parametrize("mode", [WriteMode.NONE, WriteMode.COMPANION, WriteMode.IN_PLACE])
    def test_output_dir_is_only_valid_with_directory_mode(self, mode):
        with pytest.raises(ValueError, match="output_dir"):
            SnapshotOptions(write_mode=mode, output_dir="out")


class TestDelta:
    def test_empty_delta_has_no_changes(self):
        assert Delta().has_changes is False

    @pytest.mark.parametrize("field_name, value", [
        ("added", [PackageChange("numpy", new_version="2.0")]),
        ("removed", [PackageChange("numpy", old_version="1.0")]),
        ("version_changes", [PackageChange("numpy", "1.0", "2.0")]),
        ("python_version", ("3.11", "3.12")),
        ("gpu", (None, {"has_gpu": True})),
        ("findings_appeared", [("yanked", "numpy", "1.0")]),
        ("findings_resolved", [("yanked", "numpy", "1.0")]),
    ])
    def test_any_field_counts_as_a_change(self, field_name, value):
        assert Delta(**{field_name: value}).has_changes is True

    def test_lists_are_not_shared_between_instances(self):
        first, second = Delta(), Delta()
        first.added.append(PackageChange("numpy", new_version="2.0"))
        assert second.added == []


class TestTargetResults:
    def test_defaults_describe_a_file_with_no_notebooks(self):
        for cls in (ScanResult, SnapshotResult, CheckResult):
            result = cls(target="a.ipynb")
            assert result.kind == TargetKind.FILE and result.notebooks == [] and result.failed == []

    def test_failed_lists_only_notebooks_with_an_error(self):
        good = NotebookScan(path="good.ipynb", report=_report("good.ipynb"))
        bad = NotebookScan(path="bad.ipynb", report=_report("bad.ipynb"), error="corrupt JSON")
        result = ScanResult(target="repo", kind=TargetKind.DIRECTORY, notebooks=[good, bad])
        assert result.failed == [bad]

    def test_check_failed_ignores_a_missing_manifest(self):
        nothing_to_check = NotebookCheck(path="a.ipynb", manifest_found=False)
        unreadable = NotebookCheck(path="b.ipynb", manifest_found=True, error="manifest is not a dict")
        result = CheckResult(target="repo", kind=TargetKind.DIRECTORY, notebooks=[nothing_to_check, unreadable])
        assert result.failed == [unreadable]

    def test_snapshot_manifest_comes_from_the_drift_report(self):
        drift = _drift_report()
        snap = NotebookSnapshot(path="a.ipynb", report=_report(), drift_report=drift)
        assert snap.manifest is drift.manifest
        assert NotebookSnapshot(path="a.ipynb", report=_report()).manifest is None

    def test_check_options_default_has_no_root_dir(self):
        assert CheckOptions().root_dir is None

    def test_environment_freeze_lines_default_to_empty_and_unshared(self):
        first = Environment(frozen_env={}, pkg_dist_map={})
        second = Environment(frozen_env={}, pkg_dist_map={})
        first.raw_full_freeze.append("numpy==2.0")
        assert second.raw_full_freeze == []


def test_package_root_exports_the_public_types():
    assert set(steady_py.__all__) <= set(dir(steady_py))
    assert steady_py.SnapshotOptions is SnapshotOptions



