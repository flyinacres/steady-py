"""F4: hand-written site directories. Each distribution is a dist-info folder plus the module files
it lists, so conda stubs, legacy versions, local tags, direct references, namespace packages and
shadowing need no build tool. Consumed only through a subprocess, with the site dirs on PYTHONPATH
in order (earlier wins): the tool runs `pip freeze` in a subprocess, which never sees in-process
sys.path edits."""
import json
import os
import re
from pathlib import Path
from typing import Iterable, Optional


class SiteDir:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def add(self, name: str, version: str, *, requires: Iterable[str] = (), extras: Iterable[str] = (),
            modules: Optional[Iterable[str]] = None, top_level: bool = True, installer: str = "pip",
            direct_url: Optional[dict] = None, metadata_only: bool = False) -> Path:
        """Writes `name` at `version`, verbatim. `modules` are file paths relative to the site dir
        (default: one package named after the project). `metadata_only` writes METADATA and
        INSTALLER alone, as conda-forge's stub dist-infos do."""
        dist_info = self.path / f"{re.sub(r'[-_.]+', '_', name)}-{version}.dist-info"
        dist_info.mkdir()
        lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
        lines += [f"Provides-Extra: {e}" for e in extras] + [f"Requires-Dist: {r}" for r in requires]
        files = {"METADATA": "\n".join(lines) + "\n", "INSTALLER": installer + "\n"}
        if not metadata_only:
            modules = list(modules) if modules is not None else [f"{re.sub(r'[-.]+', '_', name).lower()}/__init__.py"]
            for module in modules:
                (self.path / module).parent.mkdir(parents=True, exist_ok=True)
                (self.path / module).touch()
            if top_level:
                files["top_level.txt"] = "".join(f"{t}\n" for t in sorted({Path(m).parts[0].removesuffix('.py') for m in modules}))
            if direct_url is not None:
                files["direct_url.json"] = json.dumps(direct_url)
            record = [*modules, *(f"{dist_info.name}/{f}" for f in [*files, "RECORD"])]
            files["RECORD"] = "".join(f"{r},,\n" for r in record)  # no hashes: nothing reads them
        for filename, text in files.items():
            (dist_info / filename).write_text(text, encoding="utf-8")
        return dist_info


def pythonpath(*sites: SiteDir) -> dict:
    """The environment entry that puts `sites` first on sys.path, in order."""
    return {"PYTHONPATH": os.pathsep.join(str(s.path) for s in sites)}


# Recurring shapes, as keyword arguments to SiteDir.add.

CONDA_STUB = {"installer": "conda", "metadata_only": True}


def vcs_ref(url: str, commit: str, subdirectory: Optional[str] = None) -> dict:
    ref = {"url": url, "vcs_info": {"vcs": "git", "commit_id": commit}}
    return {"direct_url": {**ref, "subdirectory": subdirectory} if subdirectory else ref}


def file_url(path: Path) -> dict:
    return {"direct_url": {"url": Path(path).resolve().as_uri(), "archive_info": {}}}
