"""Capturing the creator's installed environment."""
import json
import os
import re
import subprocess

import pytest

from tests.support.envs import uninstall
from tests.support.markers import finding
from tests.support.notebooks import Notebook, code
from tests.support.outcomes import cell2_text, manifest
from tests.support.runner import run_in
from tests.support.sites import CONDA_STUB, SiteDir, file_url, pythonpath, vcs_ref

COMMIT = "a" * 40


@pytest.mark.venv
@finding("E6")
def test_legacy_version_is_recorded_verbatim(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    site.add("legacypkg", "0.8.1ubuntu1")
    outcome = run_in(base_venv, "scan", Notebook(code("import legacypkg")).write(tmp_path), env=pythonpath(site))
    assert outcome.exit_code in (0, 1), outcome.log
    assert outcome.dependency("legacypkg")["version"] == "0.8.1ubuntu1"


@pytest.mark.venv
@finding("E1")
@pytest.mark.parametrize("verb", [["scan"], ["snapshot", "--output"]], ids=["scan", "snapshot"])
def test_capture_needs_no_pip(tmp_path, fresh_venv, pypi, verb):
    # Capture once ran pip freeze, and a venv without pip gave an empty "verified" manifest.
    pypi.allow_unknown()  # validation's lookups aren't under test
    uninstall(fresh_venv, "pip")  # no pip module, as in a uv venv
    outcome = run_in(fresh_venv, verb[0], Notebook(code("import packaging")).write(tmp_path), *verb[1:])
    assert outcome.exit_code in (0, 1), outcome.log
    assert "packaging" in outcome.pins()



def _run(venv, tmp_path, source, *sites, verb="scan", options=(), directory=False):
    """`verb` on a one-cell notebook, or on its directory, with `sites` first on sys.path."""
    nb = Notebook(code(source)).write(tmp_path / "nb")
    outcome = run_in(venv, verb, nb.parent if directory else nb, *options, env=pythonpath(*sites))
    assert outcome.exit_code in (0, 1), outcome.log
    return outcome


@pytest.mark.venv
@finding("E3")
def test_pin_is_the_copy_that_imports(tmp_path, base_venv):
    active, shadowed = SiteDir(tmp_path / "active"), SiteDir(tmp_path / "shadowed")
    active.add("sixlike", "1.17.0")
    shadowed.add("sixlike", "1.16.0", **file_url(tmp_path / "sixlike-1.16.0-py3-none-any.whl"))
    assert _run(base_venv, tmp_path, "import sixlike", active, shadowed).pins() == {"sixlike": "1.17.0"}


@pytest.mark.venv
@finding("E2a")
def test_local_file_install_is_reported_not_pinned(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    site.add("wheelpkg", "2.0", **file_url(tmp_path / "wheelpkg-2.0-py3-none-any.whl"))
    outcome = _run(base_venv, tmp_path, "import wheelpkg", site)
    assert "wheelpkg" not in outcome.pins()  # a private project can share a PyPI name
    assert outcome.warnings(about="wheelpkg")


@pytest.mark.venv
@finding("E2a")
def test_local_file_the_notebook_installs_itself_raises_no_warning(tmp_path, base_venv):
    # The install line carries the file into raw_installs; it has its own notice, so no exit 1.
    wheel = tmp_path / "wheelpkg-2.0-py3-none-any.whl"
    site = SiteDir(tmp_path / "site")
    site.add("wheelpkg", "2.0", **file_url(wheel))
    outcome = _run(base_venv, tmp_path, f"%pip install {wheel.as_posix()}\nimport wheelpkg", site)
    assert outcome.exit_code == 0, outcome.log
    assert not outcome.warnings(type="local_file_import")


@pytest.mark.venv
@finding("E2b")
def test_conda_built_package_is_pinned_with_a_notice(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    site.add("condapkg", "1.3", installer="conda", **file_url(tmp_path / "conda-bld" / "condapkg"))
    outcome = _run(base_venv, tmp_path, "import condapkg", site)
    assert outcome.pins() == {"condapkg": "1.3"}
    assert outcome.notices(about="condapkg")


@pytest.mark.venv
@finding("E5")
def test_monorepo_packages_keep_their_subdirectories(tmp_path, base_venv, pypi):
    site = SiteDir(tmp_path / "site")
    for name in ("alpha", "beta"):
        site.add(name, "1.0", **vcs_ref("https://example.com/mono.git", COMMIT, subdirectory=name))
    outcome = _run(base_venv, tmp_path, "import alpha, beta", site, verb="snapshot", options=["--output"])
    raw = manifest(outcome.written[0])["raw_installs"]
    assert sorted(r["spec"].split("#subdirectory=")[-1] for r in raw) == ["alpha", "beta"]


@pytest.mark.venv
@finding("E7")
def test_directory_scan_lists_only_missing_packages_as_missing(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    site.add("gitpkg", "1.0", **vcs_ref("https://example.com/g.git", COMMIT))
    site.add("condapkg", "1.3", installer="conda", **file_url(tmp_path / "conda-bld" / "condapkg"))
    outcome = _run(base_venv, tmp_path, "import gitpkg, condapkg, absentmod", site, directory=True)
    assert set(outcome.summary()["missing_packages"]) == {"absentmod"}


@pytest.mark.venv
@finding("E8")
def test_cell2_claims_an_install_only_for_what_it_installs(tmp_path, base_venv, pypi):
    # Text is the contract here: a false claim in Cell 2 misleads whoever runs the notebook.
    site = SiteDir(tmp_path / "site")
    for name in ("installed", "guarded", "scripted"):
        site.add(f"git{name}", "1.0", **vcs_ref(f"https://example.com/{name}.git", COMMIT))
    source = "import gitinstalled\ntry:\n    import gitguarded\nexcept ImportError:\n    pass"
    nb = Notebook(code(source), code("%%writefile train.py\nimport gitscripted")).write(tmp_path / "nb")
    outcome = run_in(base_venv, "snapshot", nb, "--output", env=pythonpath(site))
    assert outcome.exit_code in (0, 1), outcome.log
    assert [r for r in manifest(outcome.written[0])["raw_installs"] if "installed.git" in r["spec"]]  # control
    lines = {name: [line for line in cell2_text(outcome.written[0]).splitlines() if f"# git{name} " in line]
             for name in ("guarded", "scripted")}
    assert all(found and not any("is installed from" in line for line in found) for found in lines.values()), lines


@pytest.mark.venv
@finding("K6")
def test_opencv_pin_is_the_variant_that_imports(tmp_path, base_venv):
    first, second = SiteDir(tmp_path / "first"), SiteDir(tmp_path / "second")
    first.add("opencv-python", "4.10.0.84", modules=["cv2/__init__.py"])
    second.add("opencv-python-headless", "4.8.0.76", modules=["cv2/__init__.py"])
    assert _run(base_venv, tmp_path, "import cv2", first, second).pins() == {"opencv-python": "4.10.0.84"}


@pytest.mark.venv
@finding("C1")
def test_conda_forge_opencv_maps_to_a_pypi_release(tmp_path, base_venv, pypi):
    for name in ("opencv-python", "opencv-python-headless"):
        pypi.add(name, {"4.10.0.84": {}, "5.0.0.90": {}, "5.0.0.93": {}})
    site = SiteDir(tmp_path / "site")
    for name in ("opencv_python", "opencv_python_headless"):  # the recipe writes both in every build
        site.add(name, "5.0.0", **CONDA_STUB)
    outcome = _run(base_venv, tmp_path, "import cv2", site, verb="snapshot", options=["--output"])
    assert set(outcome.pins().values()) == {"5.0.0.93"}  # the newest PyPI release of the conda version
    assert outcome.notices(about="opencv")
    check = run_in(base_venv, "check", outcome.written[0], env=pythonpath(site))
    assert check.report is not None, check.log
    assert check.findings(signal="removed") == []


@pytest.mark.venv
@finding("DG5")
def test_warns_when_most_imports_are_installed_elsewhere(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    site.add("herepkg", "1.0")
    outcome = _run(base_venv, tmp_path, "import herepkg, gonea, goneb", site)
    assert outcome.warnings(type="other_interpreter")
    half = _run(base_venv, tmp_path / "half", "import herepkg, gonea", site)  # half isn't most
    assert not half.warnings(type="other_interpreter")


# pip freeze as the oracle for importlib.metadata capture (working_rules.md §2.4): every line freeze
# prints, capture pins identically. Freeze hides pip (and setuptools and wheel before Python 3.12)
# and prints editables as `-e` lines, so capture may add those and nothing else. pip before 26
# omits an archive's hash from its freeze line, so a hash only capture carries is ignored.
_ORACLE = """
import json, re, subprocess, sys
from steady_py import installed, util
freeze = {}
out = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, check=True).stdout
for line in out.splitlines():
    if line and not line.startswith(("#", "-")):
        freeze[util.canonicalize_pkg_name(re.split(r" @ |===|==", line, maxsplit=1)[0])] = line
print(json.dumps({"freeze": freeze, "capture": installed.read_installed()}))
"""


def _without_hash(pin: str) -> str:
    return re.sub(r"#?\bsha\d+=[0-9a-f]+&?", lambda m: "#" if m.group(0).endswith("&") else "", pin)


@pytest.mark.venv
def test_capture_agrees_with_pip_freeze(tmp_path, base_venv):
    first, second = SiteDir(tmp_path / "first"), SiteDir(tmp_path / "second")
    first.add("sixlike", "1.17.0")
    second.add("sixlike", "1.16.0", **file_url(tmp_path / "sixlike-1.16.0-py3-none-any.whl"))  # shadowed
    for name in ("alpha", "beta"):
        first.add(name, "1.0", **vcs_ref("https://example.com/mono.git", COMMIT, subdirectory=name))
    first.add("legacypkg", "0.8.1ubuntu1")
    first.add("hashpkg", "2.0", direct_url={"url": "https://example.com/hashpkg-2.0.tar.gz",
                                             "archive_info": {"hashes": {"sha256": "ab" * 32}}})
    first.add("oldhashpkg", "2.0", direct_url={"url": "https://example.com/oldhashpkg-2.0.tar.gz",
                                                "archive_info": {"hash": "sha256=" + "cd" * 32}})
    first.add("condapkg", "1.3", installer="conda", **file_url(tmp_path / "conda-bld" / "condapkg"))
    first.add("dirpkg", "0.1", direct_url={"url": (tmp_path / "src" / "dirpkg").as_uri(), "dir_info": {}})
    first.add("editpkg", "0.1", direct_url={"url": (tmp_path / "src" / "editpkg").as_uri(),
                                             "dir_info": {"editable": True}})
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    result = subprocess.run([str(base_venv.python), "-c", _ORACLE], capture_output=True, text=True,
                            encoding="utf-8", env={**env, **pythonpath(first, second)})
    assert result.returncode == 0, result.stderr
    found = json.loads(result.stdout)
    freeze, capture = found["freeze"], found["capture"]
    assert {k: capture.get(k) if "#sha" in line else _without_hash(capture.get(k, "")) for k, line in freeze.items()} == freeze
    assert set(capture) - set(freeze) <= {"pip", "setuptools", "wheel", "editpkg"}
    assert capture["editpkg"] == f"editpkg @ {(tmp_path / 'src' / 'editpkg').as_uri()}"
