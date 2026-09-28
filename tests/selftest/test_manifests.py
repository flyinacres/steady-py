"""A snapshot is reused across callers, each caller's extra cells come after the original ones, and an
altered manifest still passes steady-py's own integrity check."""
import json
from importlib.metadata import version

from tests.support.manifests import altered, snapshotted
from tests.support.notebooks import code
from tests.support.outcomes import manifest
from tests.support.runner import run


def test_snapshot_is_shared_and_extra_cells_are_appended(tmp_path, pypi):
    pypi.add("packaging", {version("packaging"): {}})
    first = snapshotted(tmp_path / "a", code("import packaging  # selftest"), extra=[code("x = 1")])
    second = snapshotted(tmp_path / "b", code("import packaging  # selftest"), extra=[code("y = 2")])
    assert manifest(first) == manifest(second)  # same snapshot: even generated_at matches
    sources = ["".join(c["source"]) for c in json.loads(second.read_text(encoding="utf-8"))["cells"]]
    assert sources[-2:] == ["import packaging  # selftest", "y = 2"]


def test_altered_manifest_passes_the_integrity_check(tmp_path, pypi):
    pypi.add("packaging", {version("packaging"): {}})
    path = altered(snapshotted(tmp_path, code("import packaging  # selftest")),
                   lambda m: m.update(custom_sourced=["packaging"]))
    assert manifest(path)["custom_sourced"] == ["packaging"]
    outcome = run("check", path)
    assert outcome.findings(signal="tampered") == [], outcome.log
