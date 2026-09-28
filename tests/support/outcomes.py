"""F2: what one run of the tool produced, and the only code that knows the JSON report's shape.
Accessors raise LookupError or ValueError for harness misuse, never AssertionError, so a known_bug
xfail can't pass because of a broken test. Tests assert exit_code before using accessors."""
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from packaging.utils import canonicalize_name


@dataclass
class Outcome:
    exit_code: int
    stdout: str
    log: str
    report: Optional[dict]

    @property
    def written(self) -> list:
        """Paths the report says were written."""
        return [Path(p) for p in _strings((self.report or {}).get("artifacts_written"))]

    def _single(self) -> dict:
        if self.report is None:
            raise LookupError(f"no JSON report (exit {self.exit_code}); log:\n{self.log}")
        if self.report.get("mode") == "batch":
            raise ValueError("directory report: a per-notebook view isn't built yet")
        return self.report

    def dependencies(self) -> list:
        report = self._single()
        return report["manifest"]["dependencies"] if report["mode"] == "check_drift" else report["dependencies"]

    def pins(self) -> dict:
        """Canonical name to version for entries Cell 2 installs. A commented entry is not installed;
        the comment is the only discriminator today (status is unreliable, P6)."""
        return {canonicalize_name(d["name"]): d["version"] for d in self.dependencies()
                if d.get("version") and d.get("comment") is None}

    def dependency(self, name: str) -> Optional[dict]:
        key = canonicalize_name(name)
        return next((d for d in self.dependencies() if canonicalize_name(d["name"]) == key), None)

    def warnings(self, type: Optional[str] = None) -> list:
        """Match on DiagnosticEvent.type only. Cell numbers are asserted only by the cell-numbering
        test (P7), since today's numbering is itself a finding."""
        return [w for w in self._single().get("warnings", []) if type in (None, w["type"])]

    def notices(self, type: Optional[str] = None) -> list:
        return [n for n in self._single().get("notices", []) if type in (None, n["type"])]

    def delta(self) -> Optional[dict]:
        return self._single().get("delta")


def _strings(value) -> list:
    if isinstance(value, str):
        return [value]
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    return [s for item in items for s in _strings(item)]


def manifest(path: Path) -> dict:
    """The STEADY_PY_MANIFEST literal in a written notebook, found without steady-py's extractor
    (which has its own bug, K1): each code cell parsed alone, top-level assignments only."""
    found = []
    for cell in json.loads(Path(path).read_text(encoding="utf-8"))["cells"]:
        if cell["cell_type"] != "code":
            continue
        try:
            body = ast.parse("".join(cell["source"])).body
        except SyntaxError:
            continue  # IPython syntax; Cell 2 is plain Python
        found += [node.value for node in body if isinstance(node, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "STEADY_PY_MANIFEST" for t in node.targets)]
    if len(found) != 1:
        raise LookupError(f"expected one STEADY_PY_MANIFEST assignment in {path}, found {len(found)}")
    return ast.literal_eval(found[0])
