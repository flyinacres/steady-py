"""How install lines in the creator's notebook are harvested."""
import shutil
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.utils import canonicalize_name

from tests.support.markers import finding, known_bug
from tests.support.notebooks import Notebook, code
from tests.support.outcomes import manifest
from tests.support.runner import run, run_in
from tests.support.sites import SiteDir, pythonpath

SAVED = Path(__file__).resolve().parents[1] / "fixtures" / "unit"


def _scan(tmp_path, *sources):
    outcome = run("scan", Notebook(*map(code, sources)).write(tmp_path))
    assert outcome.exit_code in (0, 1), outcome.log
    return outcome


@known_bug("G4", "a variable install line is harvested as a package named after the variable")
@pytest.mark.parametrize("line", ["%pip install $pkg", "!pip install {pkg}", "%pip install packaging $extra"])
def test_variable_install_line_is_a_warning_not_a_package(tmp_path, line):
    # IPython expands $name and {expr} at run time; a static scan can't know the value.
    outcome = _scan(tmp_path, "pkg = extra = 'x'", line)
    assert [d["name"] for d in outcome.dependencies() if set(d["name"]) & set("${}")] == []
    assert outcome.warnings()


# Guarded installs stay in the creator's code: Cell 2 can't know which branch applies.
GUARDED = {
    "if": ("if IN_COLAB:\n    %pip install packaging", 1),
    "if-else": (f"if IN_COLAB:\n    %pip install packaging=={version('packaging')}\nelse:\n    %pip install packaging==1.0", 2),
    "except-importerror": ("try:\n    import packaging\nexcept ImportError:\n    !pip install packaging", 1),
    "function-body": ("def setup():\n    !pip install packaging\nsetup()", 1),  # G9: call sites aren't traced
    "shell-joined": ("!test -d /content && pip install packaging", 1),
    "shell-if-block": ("%%bash\nif [ -d /content ]; then\n  pip install packaging\nfi", 1),
}


@finding("G3")
@pytest.mark.parametrize("form", GUARDED)
def test_guarded_install_is_reported_not_pinned(tmp_path, form):
    cell, lines = GUARDED[form]
    outcome = _scan(tmp_path, "IN_COLAB = False", cell)
    assert "packaging" not in outcome.pins()
    assert len(outcome.warnings(type="guarded_install")) == lines


@finding("G3")
def test_unconditional_install_beside_a_guarded_one_is_pinned(tmp_path):
    outcome = _scan(tmp_path, "if IN_COLAB:\n    %pip install packaging==1.0", f"%pip install packaging=={version('packaging')}")
    assert outcome.pins().get("packaging") == version("packaging")
    assert len(outcome.warnings(type="guarded_install")) == 1


@finding("G3")
def test_guarded_install_of_an_imported_package_pins_the_installed_version(tmp_path):
    outcome = _scan(tmp_path, "if IN_COLAB:\n    %pip install packaging==1.0", "import packaging")
    assert outcome.pins().get("packaging") == version("packaging")


@finding("G6")
def test_exclusive_branches_share_a_guard_group(tmp_path):
    guards = [line["guard"] for line in _scan(tmp_path, GUARDED["if-else"][0]).install_lines()]
    assert [g["kind"] for g in guards] == ["if", "if"]
    assert guards[0]["group"] == guards[1]["group"] and [g["branch"] for g in guards] == [0, 1]


@finding("G5")
def test_literal_get_ipython_system_call_is_harvested(tmp_path):
    assert "packaging" in _scan(tmp_path, "get_ipython().system('pip install packaging')").pins()


@known_bug("G5", "install forms other than %pip, !pip and bare pip are not harvested")
@pytest.mark.parametrize("cell", [
    pytest.param("!python -m pip install packaging", id="python-m-pip"),
    pytest.param("import sys\n!{sys.executable} -m pip install packaging", id="sys-executable"),
    pytest.param("import os\nos.system('pip install packaging')", id="os-system"),
    pytest.param("import subprocess\nsubprocess.run(['pip', 'install', 'packaging'])", id="subprocess-list"),
    pytest.param("%uv pip install packaging", id="uv-pip"),
    pytest.param("!conda run -n base pip install packaging", id="conda-run-pip"),
])
def test_every_literal_install_form_is_harvested(tmp_path, cell):
    assert "packaging" in _scan(tmp_path, cell).pins()


@known_bug("G11", "--opt=value pip flags are dropped")
def test_equals_form_flags_match_space_form(tmp_path):
    flags = [_scan(tmp_path / form, f"%pip install --index-url{sep}https://x.test/simple packaging")
             .dependency("packaging")["flags"] for form, sep in (("space", " "), ("equals", "="))]
    assert flags[0] and flags[1] == flags[0]


@pytest.mark.parametrize("line, warns", [
    pytest.param('%pip install "demo @ git+https://example.com/demo.git"', False, id="quoted"),
    pytest.param("%pip install demo @ git+https://example.com/demo.git", True, id="unquoted-is-a-pip-error"),
])
@known_bug("G12", "a PEP 508 direct reference is split into separate packages")
def test_direct_reference_is_one_requirement(tmp_path, line, warns):
    outcome = _scan(tmp_path, line)
    assert [d["name"] for d in outcome.dependencies() if d["name"] in ("@", "git+https://example.com/demo.git")] == []
    assert bool(outcome.warnings()) or not warns


@known_bug("G14", "--no-deps and a raw install's index flags are dropped")
def test_flags_that_change_what_gets_installed_reach_the_manifest(tmp_path, pypi):
    pypi.add("packaging", {version("packaging"): {}})
    source = ("%pip install --no-deps packaging\n"
              "%pip install git+https://example.com/demo.git --index-url https://x.test/simple")
    outcome = run("snapshot", Notebook(code(source)).write(tmp_path), "--output")
    assert outcome.exit_code == 0, outcome.log
    written = manifest(outcome.written[0])
    assert "--no-deps" in next(d for d in written["dependencies"] if d["name"] == "packaging")["flags"]
    assert any("https://x.test/simple" in str(raw) for raw in written["raw_installs"])


@known_bug("P4", "an editable install line vanishes without a diagnostic")
def test_editable_install_line_is_reported(tmp_path):
    assert _scan(tmp_path, "%pip install -e ./mypkg").warnings()


@pytest.mark.parametrize("line", [
    pytest.param("!conda install numpy", id="conda-control"),
    *(pytest.param(line, marks=known_bug("C2", "conda-family commands other than conda install give no notice"))
      for line in ("!mamba install numpy", "%mamba install numpy", "%micromamba install numpy",
                   "!micromamba install numpy", "!conda env update -f environment.yml")),
])
def test_conda_family_commands_get_a_notice(tmp_path, line):
    assert _scan(tmp_path, line).notices()


@known_bug("C2", "conda --file is not treated like pip -r")
def test_conda_requirements_file_is_reported_like_pip_r(tmp_path):
    pip_types = {w["type"] for w in _scan(tmp_path / "pip", "%pip install -r req.txt").warnings()}
    conda_types = {w["type"] for w in _scan(tmp_path / "conda", "!conda install --file req.txt").warnings()}
    assert pip_types and pip_types <= conda_types


@known_bug("G17", "a non-canonical install name is listed twice, plus a nameless header entry")
@pytest.mark.parametrize("name", ["Packaging", "absent_pkg_zz"])
def test_each_install_line_package_is_listed_once(tmp_path, name):
    names = [canonicalize_name(d["name"]) for d in _scan(tmp_path, f"%pip install {name}").dependencies()]
    assert "" not in names
    assert len(names) == len(set(names))


@known_bug("D5", "the auxiliary-tools header comment is parsed back as a package named ---")
def test_directory_scan_reports_no_package_named_from_display_text(tmp_path):
    shutil.copy(SAVED / "magic_sink.ipynb", tmp_path)
    outcome = run("scan", tmp_path)
    assert outcome.exit_code in (0, 1), outcome.log
    assert "---" not in outcome.summary()["missing_packages"]


# Venv tier: packages whose pip name differs from their import name, installed via site dirs.

@pytest.mark.venv
@pytest.mark.parametrize("dist, module", [
    pytest.param("python-dotenv", "dotenv", id="dotenv-control"),
    pytest.param("paddlepaddle-gpu", "paddle", id="paddle",
                 marks=known_bug("G2", "an install line and an import of the same distribution give two entries")),
])
def test_install_line_and_import_of_one_distribution_give_one_entry(tmp_path, base_venv, dist, module):
    site = SiteDir(tmp_path / "site")
    site.add(dist, "3.0.0", modules=[f"{module}/__init__.py"])
    outcome = run_in(base_venv, "scan", Notebook(code(f"%pip install {dist}\nimport {module}")).write(tmp_path / "nb"),
                     env=pythonpath(site))
    assert outcome.exit_code in (0, 1), outcome.log
    assert [canonicalize_name(d["name"]) for d in outcome.dependencies()] == [dist]


@pytest.mark.venv
@known_bug("G16", "an install line with extras turns the extra into the version")
def test_extras_install_line_pins_the_installed_version(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    site.add("tabulate", "0.9.0", extras=["widechars"], requires=["wcwidth; extra == 'widechars'"])
    source = '%pip install "tabulate[widechars]"\nimport tabulate'
    outcome = run_in(base_venv, "scan", Notebook(code(source)).write(tmp_path / "nb"), env=pythonpath(site))
    assert outcome.exit_code in (0, 1), outcome.log
    entry = outcome.dependency("tabulate") or outcome.dependency("tabulate[widechars]")
    assert entry["version"] == "0.9.0"
    assert entry["name"] == "tabulate[widechars]"
