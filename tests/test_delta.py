"""compute_delta is a pure comparison of two manifests."""
from steady_py import models
from steady_py.delta import compute_delta
from steady_py.results import PackageChange


def _manifest(deps=(), python=(3, 12), gpu=None, baseline=None):
    pins = [models.PinnedDependency(name, version) for name, version in deps]
    return models.SteadyPyManifest(python_version={"major": python[0], "minor": python[1]}, dependencies=pins,
                                gpu=gpu, generated_at="t", baseline=baseline)


def _baseline(*findings):
    return models.Baseline(findings=tuple(findings))


class TestPackages:
    def test_identical_manifests_have_no_changes(self):
        manifest = _manifest([("requests", "2.32.3")])
        delta = compute_delta(manifest, _manifest([("requests", "2.32.3")]))
        assert not delta.has_changes and delta.python_version is None and delta.gpu is None

    def test_added_removed_and_changed_are_kept_apart_and_sorted(self):
        before = _manifest([("scipy", "1.11.0"), ("pandas", "2.1.0"), ("requests", "2.32.3")])
        after = _manifest([("numpy", "2.0.1"), ("attrs", "24.1"), ("pandas", "2.2.0"), ("requests", "2.32.3")])
        delta = compute_delta(before, after)
        assert delta.added == [PackageChange("attrs", new_version="24.1"), PackageChange("numpy", new_version="2.0.1")]
        assert delta.removed == [PackageChange("scipy", old_version="1.11.0")]
        assert delta.version_changes == [PackageChange("pandas", "2.1.0", "2.2.0")]
        assert delta.has_changes

    def test_extras_are_part_of_the_name(self):
        delta = compute_delta(_manifest([("pandas", "2.1.0")]), _manifest([("pandas[test]", "2.1.0")]))
        assert [c.name for c in delta.added] == ["pandas[test]"] and [c.name for c in delta.removed] == ["pandas"]


class TestEnvironmentSettings:
    def test_a_python_change_is_reported_as_before_and_after(self):
        delta = compute_delta(_manifest(python=(3, 11)), _manifest(python=(3, 12)))
        assert delta.python_version == ("3.11", "3.12") and delta.has_changes

    def test_a_gpu_change_carries_both_settings(self):
        gpu = {"has_gpu": True, "device_name": "A100"}
        delta = compute_delta(_manifest(gpu=None), _manifest(gpu=gpu))
        assert delta.gpu == (None, gpu) and delta.has_changes

    def test_an_unchanged_gpu_is_not_reported(self):
        gpu = {"has_gpu": True, "device_name": "A100"}
        assert compute_delta(_manifest(gpu=gpu), _manifest(gpu=dict(gpu))).gpu is None


class TestBaseline:
    def test_findings_are_compared_only_when_the_fresh_manifest_has_a_baseline(self):
        before = _manifest(baseline=_baseline(("yanked", "requests", "2.32.3")))
        delta = compute_delta(before, _manifest(baseline=None))
        assert delta.baseline_compared is False
        assert delta.findings_appeared == [] and delta.findings_resolved == []

    def test_appeared_and_resolved_findings(self):
        before = _manifest(baseline=_baseline(("yanked", "requests", "2.32.3"), ("stale", "numpy")))
        after = _manifest(baseline=_baseline(("stale", "numpy"), ("conflict", "attrs", "x")))
        delta = compute_delta(before, after)
        assert delta.baseline_compared is True
        assert delta.findings_appeared == [("conflict", "attrs", "x")]
        assert delta.findings_resolved == [("yanked", "requests", "2.32.3")]
        assert delta.has_changes

    def test_a_manifest_recorded_without_a_baseline_counts_every_finding_as_new(self):
        delta = compute_delta(_manifest(baseline=None), _manifest(baseline=_baseline(("stale", "numpy"))))
        assert delta.findings_appeared == [("stale", "numpy")] and delta.findings_resolved == []

    def test_equal_baselines_are_compared_and_show_nothing(self):
        same = _baseline(("stale", "numpy"))
        delta = compute_delta(_manifest(baseline=same), _manifest(baseline=_baseline(("stale", "numpy"))))
        assert delta.baseline_compared is True and not delta.has_changes


def test_to_dict_is_json_ready():
    import json
    delta = compute_delta(
        _manifest([("pandas", "2.1.0"), ("scipy", "1.0")], python=(3, 11), baseline=_baseline(("stale", "numpy"))),
        _manifest([("pandas", "2.2.0"), ("attrs", "24.1")], python=(3, 12), baseline=_baseline()),
    )
    data = json.loads(json.dumps(delta.to_dict()))
    assert data["has_changes"] is True and data["baseline_compared"] is True
    assert data["version_changes"] == [{"name": "pandas", "old_version": "2.1.0", "new_version": "2.2.0"}]
    assert data["added"] == [{"name": "attrs", "old_version": None, "new_version": "24.1"}]
    assert data["python_version"] == {"before": "3.11", "after": "3.12"}
    assert data["findings_resolved"] == [["stale", "numpy"]]
