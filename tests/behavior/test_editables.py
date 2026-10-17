"""The creator's own in-development packages: editable installs, local modules, and imports that
come from a folder no installed package owns. All go through the one import join."""
import pytest

from tests.support.envs import install_editable, install_project, installed_version
from tests.support.gitrepo import git_repo
from tests.support.markers import finding
from tests.support.notebooks import Notebook, code
from tests.support.runner import run, run_in
from tests.support.sites import SiteDir, pythonpath

def _scan(venv, target, env=None):
    outcome = run_in(venv, "scan", target, env=env)
    assert outcome.exit_code in (0, 1), outcome.log
    return outcome


def _editable_reported(outcome, name, module):
    """An editable import: not pinned, a needs-attention warning naming it, exit 1."""
    return (outcome.exit_code == 1 and name not in outcome.pins()
            and outcome.dependency(name)["status"] == "editable"
            and outcome.warnings(type="editable_import", about=f"'{module}'"))


@pytest.mark.venv
@finding("ED3")
def test_editable_import_is_reported_as_needing_attention(tmp_path, fresh_venv):
    install_project(fresh_venv, "demo-editable", tmp_path / "projects", editable=True)
    outcome = _scan(fresh_venv, Notebook(code("import demo_editable")).write(tmp_path / "nb"))
    assert _editable_reported(outcome, "demo-editable", "demo_editable"), outcome.report


@pytest.mark.venv
@finding("ED1")
@pytest.mark.parametrize("project, module", [("demo-editable", "demo_editable"), ("pdm-demo", "pdm_demo")])
def test_an_editables_own_requirements_are_pinned(tmp_path, fresh_venv, project, module):
    # demo-editable requires packaging; pdm-demo's only requirement is the `editables` shim pdm injects.
    source = install_project(fresh_venv, project, tmp_path / "projects", editable=True)
    outcome = _scan(fresh_venv, Notebook(code(f"import {module}")).write(source / "notebooks"))
    expected = {"packaging": installed_version(fresh_venv, "packaging")} if project == "demo-editable" else {}
    assert outcome.pins() == expected


@pytest.mark.venv
@finding("ED2")
def test_file_and_directory_runs_classify_alike(tmp_path, fresh_venv):
    source = install_project(fresh_venv, "demo-editable", tmp_path / "projects", editable=True)
    (source / "rootmod").mkdir()
    (source / "rootmod" / "__init__.py").touch()  # a plain folder at the project root, not installed
    nb = Notebook(code("import demo_editable\nimport rootmod")).write(source / "notebooks")
    single, batch = _scan(fresh_venv, nb), _scan(fresh_venv, source).notebook(nb.name)
    assert single.dependencies() == batch.dependencies()
    assert single.dependency("demo-editable")["status"] == "editable"
    assert single.dependency("rootmod")["status"] == "local_module"


@pytest.mark.venv
@finding("ED4")
@pytest.mark.parametrize("project, name, module", [
    ("tabulate-ron", "tabulate-ron", "tabulate"),  # a renamed hatchling fork: no top_level.txt
    ("pdm-demo", "pdm-demo", "pdm_demo"),
])
def test_editables_without_top_level_are_joined(tmp_path, fresh_venv, project, name, module):
    install_project(fresh_venv, project, tmp_path / "projects", editable=True)
    outcome = _scan(fresh_venv, Notebook(code(f"import {module}")).write(tmp_path / "nb"))
    assert _editable_reported(outcome, name, module), outcome.report


@pytest.mark.venv
@finding("ED4")
def test_legacy_develop_install_is_joined(tmp_path, base_venv):
    site = SiteDir(tmp_path / "site")
    source = site.add_develop("legacy-dev", "0.3", tmp_path / "legacy")
    outcome = _scan(base_venv, Notebook(code("import legacy_dev")).write(tmp_path / "nb"), env=pythonpath(site, source))
    assert _editable_reported(outcome, "legacy-dev", "legacy_dev"), outcome.report


@pytest.mark.venv
@finding("ED4")
def test_writefile_import_is_joined_like_a_cell_import(tmp_path, fresh_venv):
    install_project(fresh_venv, "tabulate-ron", tmp_path / "projects", editable=True)
    outcome = _scan(fresh_venv, Notebook(code("%%writefile train.py\nimport tabulate")).write(tmp_path / "nb"))
    assert outcome.exit_code == 1
    assert outcome.warnings(type="editable_import", about="'tabulate'")


@pytest.mark.venv
@finding("ED4")
def test_module_on_a_custom_path_is_reported(tmp_path, base_venv):
    folder = tmp_path / "elsewhere"
    (folder / "pathmod").mkdir(parents=True)
    (folder / "pathmod" / "__init__.py").touch()
    outcome = _scan(base_venv, Notebook(code("import pathmod")).write(tmp_path / "nb"), env=pythonpath(folder))
    assert outcome.exit_code == 1
    assert outcome.dependency("pathmod")["status"] == "custom_path"
    assert outcome.warnings(type="custom_path_import", about="'pathmod'")


@pytest.mark.venv
@finding("ED5")
def test_vcs_editable_names_its_git_source(tmp_path, fresh_venv):
    url, commit = git_repo(tmp_path / "git", "demo-editable")
    install_editable(fresh_venv, f"{url}#egg=demo-editable")
    outcome = _scan(fresh_venv, Notebook(code("import demo_editable")).write(tmp_path / "nb"))
    assert _editable_reported(outcome, "demo-editable", "demo_editable"), outcome.report
    assert outcome.warnings(type="editable_import", about=f"@{commit}")


@pytest.mark.venv
@finding("ED6")
@pytest.mark.parametrize("installed", [True, False], ids=["installed", "control"])
def test_data_folder_does_not_hide_an_installed_package(tmp_path, base_venv, installed):
    site = SiteDir(tmp_path / "site")
    if installed:
        site.add("datasets", "2.0.0")
    (tmp_path / "nb" / "datasets").mkdir(parents=True)
    (tmp_path / "nb" / "datasets" / "train.csv").write_text("a,b\n")
    outcome = _scan(base_venv, Notebook(code("import datasets")).write(tmp_path / "nb"), env=pythonpath(site))
    if installed:
        assert outcome.pins() == {"datasets": "2.0.0"}
    else:  # with nothing installed, the folder is what `import datasets` finds
        assert outcome.dependency("datasets")["status"] == "local_module"


@finding("ED2")
def test_missing_root_module_message_names_root_dir(tmp_path):
    nb = Notebook(code("import roothelper")).write(tmp_path / "proj")
    (tmp_path / "roothelper.py").touch()
    assert run("snapshot", tmp_path, "--in-place").exit_code == 0
    (tmp_path / "roothelper.py").unlink()
    outcome = run("check", nb, "--root-dir", tmp_path)
    assert outcome.exit_code == 1
    assert "--root-dir" in outcome.findings(signal="local_module_missing")[0]["message"]
