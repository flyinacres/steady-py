"""F6: local git repositories. A saved project copied into tmp and committed, referenced as
`git+file://...@<commit>`, with `#subdirectory=` for a project inside a monorepo. The test can't
run without git (markers.unavailable)."""
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from tests.support.envs import PROJECTS
from tests.support.markers import unavailable


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
    return result.stdout.strip()


def git_repo(workdir: Path, project: str, subdirectory: Optional[str] = None) -> Tuple[str, str]:
    """tests/fixtures/projects/<project> committed as a repository in `workdir` (under
    `subdirectory` when given). Returns (the `git+file://` URL without a ref, the commit)."""
    if shutil.which("git") is None:
        unavailable("git is not installed")
    repo = Path(workdir) / "repo"
    shutil.copytree(PROJECTS / project, repo / subdirectory if subdirectory else repo)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=steady-py tests", "-c", "user.email=tests@invalid", "commit", "-q", "-m", "fixture")
    return f"git+{repo.resolve().as_uri()}", _git(repo, "rev-parse", "HEAD")
