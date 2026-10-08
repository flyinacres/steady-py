"""How install lines in the creator's notebook are harvested."""
import shutil
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.utils import canonicalize_name

from tests.support.markers import finding, known_bug
from tests.support.notebooks import Notebook, code
from tests.support.outcomes import manifest
from tests.support.runner import pip_option_table, run, run_in
from tests.support.sites import SiteDir, pythonpath

SAVED = Path(__file__).resolve().parents[1] / "fixtures" / "unit"


def _scan(tmp_path, *sources):
    outcome = run("scan", Notebook(*map(code, sources)).write(tmp_path))
    assert outcome.exit_code in (0, 1), outcome.log
    return outcome


@finding("G4")
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


@finding("G5")
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


@finding("G11")
def test_equals_form_flags_match_space_form(tmp_path):
    flags = [_scan(tmp_path / form, f"%pip install --index-url{sep}https://x.test/simple packaging")
             .dependency("packaging")["flags"] for form, sep in (("space", " "), ("equals", "="))]
    assert flags[0] and flags[1] == flags[0]


@pytest.mark.parametrize("line, warns", [
    pytest.param('%pip install "demo @ git+https://example.com/demo.git"', False, id="quoted"),
    pytest.param("%pip install demo @ git+https://example.com/demo.git", True, id="unquoted-is-a-pip-error"),
])
@finding("G12")
def test_direct_reference_is_one_requirement(tmp_path, line, warns):
    outcome = _scan(tmp_path, line)
    assert [d["name"] for d in outcome.dependencies() if d["name"] in ("@", "git+https://example.com/demo.git")] == []
    assert bool(outcome.warnings()) or not warns


@finding("G14")
def test_flags_that_change_what_gets_installed_reach_the_manifest(tmp_path, pypi):
    pypi.add("packaging", {version("packaging"): {}})
    source = ("%pip install --no-deps packaging\n"
              "%pip install git+https://example.com/demo.git --index-url https://x.test/simple")
    outcome = run("snapshot", Notebook(code(source)).write(tmp_path), "--output")
    assert outcome.exit_code == 0, outcome.log
    written = manifest(outcome.written[0])
    assert "--no-deps" in next(d for d in written["dependencies"] if d["name"] == "packaging")["flags"]
    assert any("https://x.test/simple" in str(raw) for raw in written["raw_installs"])


def _snapshot_manifest(tmp_path, pypi, *sources):
    pypi.add("packaging", {version("packaging"): {}})
    outcome = run("snapshot", Notebook(*map(code, sources)).write(tmp_path), "--output")
    assert outcome.exit_code in (0, 1), outcome.log
    return outcome, manifest(outcome.written[0])


@finding("G18")
def test_trailing_comment_is_not_harvested(tmp_path, pypi):
    outcome, written = _snapshot_manifest(tmp_path, pypi, "!pip install packaging # kaggle doesnt have it",
                                          "!pip install git+https://example.com/demo.git#egg=demo")
    assert [d["name"] for d in written["dependencies"]] == ["packaging"]
    assert [r["spec"] for r in written["raw_installs"]] == ["git+https://example.com/demo.git#egg=demo"]  # control


@finding("G19")
def test_clustered_short_flags_are_read_as_pip_reads_them(tmp_path):
    outcome = _scan(tmp_path, "%pip install -qr req.txt")
    assert outcome.warnings(type="external_requirement")
    assert [d["name"] for d in outcome.dependencies() if "req" in d["name"]] == []


@finding("CH3")
def test_named_direct_reference_is_stored_as_written(tmp_path, pypi):
    spec = "demo @ git+https://example.com/demo.git"
    _outcome, written = _snapshot_manifest(tmp_path, pypi, f'%pip install "{spec}"')
    assert [r["spec"] for r in written["raw_installs"]] == [spec]


@finding("DG8")
def test_kaggle_input_wheel_names_its_dataset(tmp_path, pypi):
    wheel = "/kaggle/input/offline-wheels/demo-1.0-py3-none-any.whl"
    outcome, written = _snapshot_manifest(tmp_path, pypi, f"!pip install --no-deps {wheel}")
    assert outcome.notices(type="kaggle_input_install", about="offline-wheels")
    assert [r["spec"] for r in written["raw_installs"]] == [wheel]
    assert [d for d in written["dependencies"] if canonicalize_name(d["name"]) == "demo"] == []


def test_option_table_matches_pips_own_parser():
    from pip._internal.commands import create_command  # pip's parser is the source of truth
    table = pip_option_table()
    for option in create_command("install").parser._get_all_options():
        for spelling in option._short_opts + option._long_opts:
            assert table.get(spelling, (None, None))[1] is option.takes_value(), spelling


@finding("P4")
def test_editable_install_line_is_reported(tmp_path):
    assert _scan(tmp_path, "%pip install -e ./mypkg").warnings()


@finding("C2")
@pytest.mark.parametrize("line", ["!conda install numpy", "!mamba install numpy", "%mamba install numpy",
                                  "%micromamba install numpy", "!micromamba install numpy",
                                  "!conda env update -f environment.yml"])
def test_conda_family_commands_get_a_notice(tmp_path, line):
    assert _scan(tmp_path, line).notices()


@finding("C2")
def test_conda_requirements_file_is_reported_like_pip_r(tmp_path):
    pip_types = {w["type"] for w in _scan(tmp_path / "pip", "%pip install -r req.txt").warnings()}
    conda_types = {w["type"] for w in _scan(tmp_path / "conda", "!conda install --file req.txt").warnings()}
    assert pip_types and pip_types <= conda_types


@finding("G17")
@pytest.mark.parametrize("name", ["Packaging", "absent_pkg_zz"])
def test_each_install_line_package_is_listed_once(tmp_path, name):
    names = [canonicalize_name(d["name"]) for d in _scan(tmp_path, f"%pip install {name}").dependencies()]
    assert "" not in names
    assert len(names) == len(set(names))


@finding("D5")
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
@finding("G16")
def test_extras_install_line_pins_the_installed_version(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    site.add("tabulate", "0.9.0", extras=["widechars"], requires=["wcwidth; extra == 'widechars'"])
    source = '%pip install "tabulate[widechars]"\nimport tabulate'
    outcome = run_in(base_venv, "scan", Notebook(code(source)).write(tmp_path / "nb"), env=pythonpath(site))
    assert outcome.exit_code in (0, 1), outcome.log
    entry = outcome.dependency("tabulate") or outcome.dependency("tabulate[widechars]")
    assert entry["version"] == "0.9.0"
    assert entry["name"] == "tabulate[widechars]"
