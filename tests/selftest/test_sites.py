"""F4 writes each shape so pip freeze and importlib.metadata, the tool's two sources of installed
state, both see it as intended. Both are run in a subprocess, as the tool will see them."""
import json
import os
import subprocess
import sys

from tests.support.sites import CONDA_STUB, SiteDir, file_url, pythonpath, vcs_ref

PROBE = """
import importlib, importlib.metadata as md, json, sys
names = sys.argv[1:]
print(json.dumps({
    "versions": {n: md.version(n) for n in names},
    "installers": {n: md.distribution(n).read_text("INSTALLER").strip() for n in names},
    "providers": {k: v for k, v in md.packages_distributions().items() if k in ("demo", "acme")},
    "origin": importlib.util.find_spec("demo").origin,
}))
"""


def _in_env(sites, *args) -> str:
    env = {**os.environ, **pythonpath(*sites)}
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=env, check=True).stdout


def test_each_shape_is_seen_as_written(tmp_path):
    first, second = SiteDir(tmp_path / "first"), SiteDir(tmp_path / "second")
    first.add("demo", "1.0", requires=["six; extra == 'fast'"], extras=["fast"])
    second.add("demo", "0.9")  # shadowed by first
    first.add("legacypkg", "0.8.1ubuntu1")
    first.add("demo-loc", "1.0+cu126")
    first.add("opencv-python", "5.0.0", **CONDA_STUB)
    first.add("fromgit", "2.0", **vcs_ref("https://example.com/r.git", "a" * 40, "sub"))
    first.add("fromfile", "3.0", **file_url(tmp_path / "fromfile-3.0-py3-none-any.whl"))
    # A namespace package shared by two distributions (P2), without top_level.txt.
    first.add("acme-cloud-storage", "2.0", modules=["acme/cloud/storage/__init__.py"], top_level=False)
    first.add("acme-proto", "4.0", modules=["acme/proto/__init__.py"], top_level=False)

    freeze = set(_in_env([first, second], "-m", "pip", "freeze").splitlines())
    assert {"demo==1.0", "legacypkg===0.8.1ubuntu1", "demo-loc==1.0+cu126", "opencv-python==5.0.0",
            f"fromgit @ git+https://example.com/r.git@{'a' * 40}#subdirectory=sub",
            f"fromfile @ {(tmp_path / 'fromfile-3.0-py3-none-any.whl').resolve().as_uri()}"} <= freeze

    seen = json.loads(_in_env([first, second], "-c", PROBE, "demo", "legacypkg", "opencv-python"))
    assert seen["versions"] == {"demo": "1.0", "legacypkg": "0.8.1ubuntu1", "opencv-python": "5.0.0"}
    assert seen["installers"]["opencv-python"] == "conda"
    assert seen["origin"].startswith(str(first.path))
    assert sorted(seen["providers"]["acme"]) == ["acme-cloud-storage", "acme-proto"]
    assert seen["providers"]["demo"] == ["demo", "demo"]  # both copies; the tool must pick the first
