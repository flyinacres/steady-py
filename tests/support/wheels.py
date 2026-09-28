"""F5: minimal wheels written directly, for tests that install stub packages. pip installs them like
any built wheel; no build backend is involved, so writing one takes milliseconds."""
import re
import zipfile
from pathlib import Path
from typing import Iterable

_WHEEL = "Wheel-Version: 1.0\nGenerator: steady-py-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"


def write_wheel(directory: Path, name: str, version: str, *, requires: Iterable[str] = (),
                extras: Iterable[str] = (), source: str = "") -> Path:
    """`name` at `version` as a py3-none-any wheel in `directory`: one package named after the
    project, whose __init__.py holds `source`. `requires` are Requires-Dist lines, verbatim."""
    module = re.sub(r"[-.]+", "_", name).lower()
    dist_info = f"{module}-{version}.dist-info"
    metadata = [f"Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    metadata += [f"Provides-Extra: {e}" for e in extras] + [f"Requires-Dist: {r}" for r in requires]
    files = {f"{module}/__init__.py": source, f"{dist_info}/METADATA": "\n".join(metadata) + "\n",
             f"{dist_info}/WHEEL": _WHEEL}
    files[f"{dist_info}/RECORD"] = "".join(f"{f},,\n" for f in [*files, f"{dist_info}/RECORD"])
    Path(directory).mkdir(parents=True, exist_ok=True)
    path = Path(directory) / f"{module}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        for filename, text in files.items():
            wheel.writestr(filename, text)
    return path
