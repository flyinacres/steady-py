"""F5: real virtual environments for the venv tier. The base venv holds only pip, steady-py (from a
wheel, not editable, so steady-py isn't an editable in the environment it inspects) and steady-py's
dependencies."""
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def create_venv(path: Path, with_pip: bool = True) -> Venv:
    _check(sys.executable, "-m", "venv", *([] if with_pip else ["--without-pip"]), path)
    return Venv(Path(path))


def build_base_venv(workdir: Path) -> Venv:
    """Builds steady-py's wheel from the repository, then installs it into a new venv."""
    _check(sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", workdir / "dist", REPO_ROOT)
    venv = create_venv(workdir / "base")
    wheel = next((workdir / "dist").glob("steady_py-*.whl"))
    _check(venv.python, "-m", "pip", "install", "-q", "--disable-pip-version-check", wheel)
    return venv
