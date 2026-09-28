"""Which pin, and which status, each dependency gets."""
import re
from importlib.metadata import version

import pytest

from tests.support.manifests import snapshotted
from tests.support.markers import known_bug
from tests.support.notebooks import Notebook, code
from tests.support.outcomes import setup_markdown
from tests.support.runner import run, run_in
from tests.support.sites import SiteDir, pythonpath

INSTALLED = version("packaging")


def _scan(tmp_path, *sources):
    outcome = run("scan", Notebook(*map(code, sources)).write(tmp_path))
    assert outcome.exit_code in (0, 1), outcome.log
    return outcome


@pytest.mark.parametrize("module, installed", [
    ("packaging", True),
    pytest.param("steady_py_absent_mod", False, marks=known_bug("P6", "not-found imports are labeled pinned")),
])
def test_status_pinned_only_for_installed_imports(tmp_path, module, installed):
    outcome = run("scan", Notebook(code(f"import {module}")).write(tmp_path))
    assert outcome.exit_code in (0, 1)
    assert (outcome.dependency(module)["status"] == "pinned") is installed


# Pin what's installed over a loose spec; an explicit == is kept as written.
@pytest.mark.parametrize("spec, warns", [
    pytest.param(">=20", False, marks=known_bug("P1", "a lower bound becomes the pin"), id="lower-bound"),
    pytest.param(">=20,<99", False, marks=known_bug("P1", "a range becomes an invalid pin"), id="range"),
    pytest.param("~=1.0", True, marks=known_bug("P1", "an unsatisfied spec becomes the pin, silently"), id="unsatisfied"),
    pytest.param(f"=={INSTALLED}", False, id="explicit"),
])
def test_loose_spec_pins_the_installed_version(tmp_path, spec, warns):
    outcome = _scan(tmp_path, f'%pip install "packaging{spec}"', "import packaging")
    assert outcome.pins()["packaging"] == INSTALLED
    assert bool(outcome.warnings(about="packaging")) is warns


def _scan_in(venv, tmp_path, site, source):
    outcome = run_in(venv, "scan", Notebook(code(source)).write(tmp_path), env=pythonpath(site))
    assert outcome.exit_code in (0, 1), outcome.log
    return outcome


@pytest.mark.venv
@known_bug("P2", "a namespace import resolves to the first distribution under the top-level name")
def test_namespace_import_pins_the_distribution_that_provides_it(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    for part in "ab":
        site.add(f"nsdemo-{part}", "1.0", modules=[f"nsdemo/{part}/__init__.py"])
    pins = _scan_in(base_venv, tmp_path, site, "from nsdemo import a\nfrom nsdemo import b").pins()
    assert {"nsdemo-a", "nsdemo-b"} <= pins.keys()


@pytest.mark.venv
@known_bug("P2", "a module two distributions provide is pinned to one of them, silently")
def test_ambiguous_import_is_reported(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    site.add("dupmod-x", "1.0", modules=["dupmod/__init__.py"])
    site.add("dupmod-y", "2.0", modules=["dupmod/__init__.py"])
    assert _scan_in(base_venv, tmp_path, site, "import dupmod").warnings(about="dupmod")


@pytest.mark.venv
@known_bug("K9", "the promoted extra depends on hash randomization")
def test_extras_promotion_is_deterministic(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    tails = ("alpha", "beta", "gamma", "delta")
    site.add("demo-x", "1.0", extras=tails, modules=[f"demo_x/{t}.py" for t in tails] + ["demo_x/__init__.py"])
    nb = Notebook(code("\n".join(f"import demo_x.{t}" for t in tails))).write(tmp_path)
    names = set()
    for seed in "1234":  # with four matching extras, four seeds agreeing by chance is unlikely
        outcome = run_in(base_venv, "scan", nb, env={**pythonpath(site), "PYTHONHASHSEED": seed})
        assert outcome.exit_code in (0, 1), outcome.log
        names.add(tuple(sorted(d["name"] for d in outcome.dependencies())))
    assert len(names) == 1, names


@pytest.mark.parametrize("line, warns", [
    pytest.param("!pip install packaging --index-url https://idx.test/simple", True,
                 marks=known_bug("CI2", "the environment's version is paired with the line's index, silently"),
                 id="unversioned"),
    pytest.param(f"!pip install packaging=={INSTALLED} --index-url https://idx.test/simple", False, id="explicit"),
])
def test_environment_version_is_not_silently_paired_with_a_lines_index(tmp_path, line, warns):
    outcome = _scan(tmp_path, line, "import packaging")
    assert bool(outcome.warnings(about="packaging")) is warns


@known_bug("LV4", "Cell 1 lists pip flag names as download URLs")
def test_setup_markdown_lists_sources_not_flags(tmp_path, pypi):
    pypi.add("packaging", {INSTALLED: {}})
    pypi.add("resolvelib", {version("resolvelib"): {}})
    nb = snapshotted(tmp_path, code(f"%pip install packaging=={INSTALLED}+cu121 --index-url https://idx.test/simple"),
                     code(f"%pip install -f https://links.test/ resolvelib=={version('resolvelib')}+cpu"),
                     code("import packaging, resolvelib"))
    text = setup_markdown(nb)
    assert "https://idx.test/simple" in text and "https://links.test/" in text
    assert re.findall(r"`(-[^`]*)`", text) == []
    assert all("index" not in line.lower() for line in text.splitlines() if "links.test" in line)  # -f is a page
