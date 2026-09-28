"""F8: Docker scenarios for conditions a host venv can't reproduce. The first: a non-root user
running an admin-owned (root-owned) system Python, as on a shared JupyterHub or course server,
where pip falls back to a user-site install the running interpreter can't see."""
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from tests.support.outcomes import Outcome
from tests.support.runner import install_outcome

IMAGE = "python:3.11-slim"
PYTHON = (3, 11)


def unavailable() -> Optional[str]:
    """Why Docker can't run here, or None."""
    if shutil.which("docker") is None:
        return "docker is not on PATH"
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return "the Docker daemon is not running"
    return None


def run_as_user(work: Path, command: str, setup: str = "") -> Outcome:
    """Runs `setup` as root, then `command` as a non-root user, in a fresh container with `work`
    mounted at /work. pip installs offline from /work/wheels. Returns the command's Outcome, with
    an InstallResult report when the command ran the installer script."""
    script = "\n".join([
        "set -e",
        "chmod -R a+rX /work",  # pytest's temp directories are private to the host user
        setup,
        "useradd -m runner",
        "cd /work",
        "set +e",
        f"runuser -u runner -- env PIP_NO_INDEX=1 PIP_FIND_LINKS=/work/wheels {command}",
    ])
    mount = f"type=bind,source={Path(work).resolve()},target=/work"  # --mount: no drive-letter colon to parse
    result = subprocess.run(["docker", "run", "--rm", "--mount", mount, IMAGE, "bash", "-c", script],
                            capture_output=True, text=True, encoding="utf-8")
    return install_outcome(result.returncode, result.stdout, result.stderr)


def root_pip(*requirements: str) -> str:
    """A setup line installing `requirements` as root into the system Python."""
    return "pip install -q --disable-pip-version-check --no-index --find-links /work/wheels " + " ".join(requirements)


def require_scenario(reproduced: bool, outcome: Outcome) -> None:
    """Raises RuntimeError, not AssertionError, when the container didn't set up the condition
    under test, so a broken scenario can't pass as a known bug's expected failure."""
    if not reproduced:
        raise RuntimeError(f"Docker scenario not reproduced (exit {outcome.exit_code}):\n{outcome.stdout}\n{outcome.log}")
