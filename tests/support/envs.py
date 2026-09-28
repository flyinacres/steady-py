"""F5: real virtual environments for the venv tier, installed offline from the wheelhouse. A venv
holds only pip, steady-py (from a wheel, not editable, so steady-py isn't an editable in the
environment it inspects), steady-py's dependencies, and whatever the test installs."""
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
WHEELHOUSE = REPO_ROOT / "tests" / ".wheelhouse"
PROJECTS = REPO_ROOT / "tests" / "fixtures" / "projects"
# Build backends for the stub projects, plus steady-py's own build backend and dependencies.
TOOLING = ["setuptools>=64", "wheel", "hatchling", "pdm-backend", "editables", "packaging", "resolvelib"]
OFFLINE = ("--no-index", "--find-links", str(WHEELHOUSE))


@dataclass(frozen=True)
class Venv:
    path: Path

    @property
    def python(self) -> Path:
        return self.path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _check(*cmd) -> None:
    result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))} failed:\n{result.stdout}{result.stderr}")


def _pip(python, *args) -> None:
    _check(python, "-m", "pip", "--disable-pip-version-check", "-q", *args)


def ensure_wheelhouse() -> Optional[str]:
    """None when all of TOOLING resolves from the wheelhouse, downloading it once if not;
    otherwise the reason, for a skip."""
    try:
        _pip(sys.executable, "download", *OFFLINE, "-d", WHEELHOUSE, *TOOLING)
        return None
    except RuntimeError:
        pass
    try:
        _pip(sys.executable, "download", "-d", WHEELHOUSE, *TOOLING)
        return None
    except RuntimeError as error:
        return f"wheelhouse {WHEELHOUSE} is incomplete and could not be downloaded: {error}"


def build_steady_py(dist: Path) -> Path:
    """steady-py's wheel, built offline from a copy of the packaging inputs: setuptools builds in
    the source tree, which would litter the repository and collide between parallel workers.
    Returns the directory holding the wheel."""
    source = Path(dist).parent / "steady-py-source"
    source.mkdir(parents=True, exist_ok=True)
    for item in REPO_ROOT.iterdir():
        if item.name == "src" or item.name in ("pyproject.toml", "setup.cfg", "setup.py") \
                or item.name.startswith(("README", "LICENSE")):
            (shutil.copytree if item.is_dir() else shutil.copy2)(item, source / item.name)
    _pip(sys.executable, "wheel", "--no-deps", *OFFLINE, "-w", dist, source)
    return dist


def create_venv(path: Path, with_pip: bool = True) -> Venv:
    _check(sys.executable, "-m", "venv", *([] if with_pip else ["--without-pip"]), path)
    return Venv(Path(path))


def steady_venv(path: Path, dist: Path) -> Venv:
    """A new venv with steady-py from `dist` and its dependencies from the wheelhouse."""
    venv = create_venv(path)
    _pip(venv.python, "install", *OFFLINE, "--find-links", dist, "steady-py")
    return venv


def install_project(venv: Venv, name: str, workdir: Path, editable: bool = False) -> Path:
    """Copies tests/fixtures/projects/<name> into `workdir` (builds write into the source tree)
    and installs it offline. Returns the copy."""
    source = Path(shutil.copytree(PROJECTS / name, Path(workdir) / name))
    _pip(venv.python, "install", *OFFLINE, *(["-e"] if editable else []), source)
    return source


def uninstall(venv: Venv, *names: str) -> None:
    """Removes distributions from `venv`. Uninstalling pip itself gives a venv like uv's (E1)."""
    _check(venv.python, "-m", "pip", "uninstall", "-y", "-q", *names)
