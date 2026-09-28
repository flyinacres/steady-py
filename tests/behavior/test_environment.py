"""Capturing the creator's installed environment."""
import pytest

from tests.support.envs import uninstall
from tests.support.markers import known_bug
from tests.support.notebooks import Notebook, code
from tests.support.outcomes import cell2_text, manifest
from tests.support.runner import run_in
from tests.support.sites import CONDA_STUB, SiteDir, file_url, pythonpath, vcs_ref

COMMIT = "a" * 40


@pytest.mark.venv
@known_bug("E6", "a non-PEP 440 version is stored with the stray '=' of pip freeze's '==='")
def test_legacy_version_is_recorded_verbatim(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    site.add("legacypkg", "0.8.1ubuntu1")
    outcome = run_in(base_venv, "scan", Notebook(code("import legacypkg")).write(tmp_path), env=pythonpath(site))
    assert outcome.exit_code in (0, 1), outcome.log
    assert outcome.dependency("legacypkg")["version"] == "0.8.1ubuntu1"


@pytest.mark.venv
@known_bug("E1", "a failed pip freeze gives an empty 'verified' manifest and exit 0")
@pytest.mark.parametrize("verb", [["scan"], ["snapshot", "--output"]], ids=["scan", "snapshot"])
def test_failed_freeze_stops_the_run_and_writes_nothing(tmp_path, fresh_venv, verb):
    uninstall(fresh_venv, "pip")  # no pip module, as in a uv venv
    outcome = run_in(fresh_venv, verb[0], Notebook(code("import packaging")).write(tmp_path), *verb[1:])
    assert outcome.exit_code == 2, outcome.log
    assert list(tmp_path.glob("*_merged.ipynb")) == []



def _run(venv, tmp_path, source, *sites, verb="scan", options=(), directory=False):
    """`verb` on a one-cell notebook, or on its directory, with `sites` first on sys.path."""
    nb = Notebook(code(source)).write(tmp_path / "nb")
    outcome = run_in(venv, verb, nb.parent if directory else nb, *options, env=pythonpath(*sites))
    assert outcome.exit_code in (0, 1), outcome.log
    return outcome


@pytest.mark.venv
@known_bug("E3", "the direct-reference overlay pins a shadowed copy")
def test_pin_is_the_copy_that_imports(tmp_path, base_venv):
    active, shadowed = SiteDir(tmp_path / "active"), SiteDir(tmp_path / "shadowed")
    active.add("sixlike", "1.17.0")
    shadowed.add("sixlike", "1.16.0", **file_url(tmp_path / "sixlike-1.16.0-py3-none-any.whl"))
    assert _run(base_venv, tmp_path, "import sixlike", active, shadowed).pins() == {"sixlike": "1.17.0"}


@pytest.mark.venv
@known_bug("E2a", "a package installed from a local file is dropped, silently")
def test_local_file_install_is_reported_not_pinned(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    site.add("wheelpkg", "2.0", **file_url(tmp_path / "wheelpkg-2.0-py3-none-any.whl"))
    outcome = _run(base_venv, tmp_path, "import wheelpkg", site)
    assert "wheelpkg" not in outcome.pins()  # a private project can share a PyPI name
    assert outcome.warnings(about="wheelpkg")


@pytest.mark.venv
@known_bug("E2b", "a conda-built package is dropped")
def test_conda_built_package_is_pinned_with_a_notice(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    site.add("condapkg", "1.3", installer="conda", **file_url(tmp_path / "conda-bld" / "condapkg"))
    outcome = _run(base_venv, tmp_path, "import condapkg", site)
    assert outcome.pins() == {"condapkg": "1.3"}
    assert outcome.notices(about="condapkg")


@pytest.mark.venv
@known_bug("E5", "#subdirectory= is lost, so a monorepo's packages merge into one install")
def test_monorepo_packages_keep_their_subdirectories(tmp_path, base_venv, pypi):
    site = SiteDir(tmp_path / "site")
    for name in ("alpha", "beta"):
        site.add(name, "1.0", **vcs_ref("https://example.com/mono.git", COMMIT, subdirectory=name))
    outcome = _run(base_venv, tmp_path, "import alpha, beta", site, verb="snapshot", options=["--output"])
    raw = manifest(outcome.written[0])["raw_installs"]
    assert sorted(r.split("#subdirectory=")[-1] for r in raw) == ["alpha", "beta"]


@pytest.mark.venv
@known_bug("E7", "a directory scan lists git and conda packages as missing")
def test_directory_scan_lists_only_missing_packages_as_missing(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    site.add("gitpkg", "1.0", **vcs_ref("https://example.com/g.git", COMMIT))
    site.add("condapkg", "1.3", installer="conda", **file_url(tmp_path / "conda-bld" / "condapkg"))
    outcome = _run(base_venv, tmp_path, "import gitpkg, condapkg, absentmod", site, directory=True)
    assert set(outcome.summary()["missing_packages"]) == {"absentmod"}


@pytest.mark.venv
@known_bug("E8", "Cell 2 says a direct reference is installed when nothing installs it")
def test_cell2_claims_an_install_only_for_what_it_installs(tmp_path, base_venv, pypi):
    # Text is the contract here: a false claim in Cell 2 misleads whoever runs the notebook.
    site = SiteDir(tmp_path / "site")
    for name in ("installed", "guarded", "scripted"):
        site.add(f"git{name}", "1.0", **vcs_ref(f"https://example.com/{name}.git", COMMIT))
    source = "import gitinstalled\ntry:\n    import gitguarded\nexcept ImportError:\n    pass"
    nb = Notebook(code(source), code("%%writefile train.py\nimport gitscripted")).write(tmp_path / "nb")
    outcome = run_in(base_venv, "snapshot", nb, "--output", env=pythonpath(site))
    assert outcome.exit_code in (0, 1), outcome.log
    assert [r for r in manifest(outcome.written[0])["raw_installs"] if "installed.git" in r]  # control
    lines = {name: [line for line in cell2_text(outcome.written[0]).splitlines() if f"# git{name} " in line]
             for name in ("guarded", "scripted")}
    assert all(found and not any("is installed from" in line for line in found) for found in lines.values()), lines


@pytest.mark.venv
@known_bug("K6", "the OpenCV variant comes from pip list, not from the copy that imports")
def test_opencv_pin_is_the_variant_that_imports(tmp_path, base_venv):
    first, second = SiteDir(tmp_path / "first"), SiteDir(tmp_path / "second")
    first.add("opencv-python", "4.10.0.84", modules=["cv2/__init__.py"])
    second.add("opencv-python-headless", "4.8.0.76", modules=["cv2/__init__.py"])
    assert _run(base_venv, tmp_path, "import cv2", first, second).pins() == {"opencv-python": "4.10.0.84"}


@pytest.mark.venv
@known_bug("C1", "a conda-forge OpenCV stub's version is pinned verbatim and reported removed")
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
