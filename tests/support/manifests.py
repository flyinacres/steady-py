"""Notebooks that carry a manifest, made by a real snapshot (design §4.5). The snapshot of a given
set of cells runs once per session; each caller gets a copy with its own cells appended, as a
creator's later edits would be. `altered` is the one place tests change a manifest's content."""
import ast
import copy
import json
from pathlib import Path
from typing import Callable, Iterable

from steady_py.models import SteadyPyManifest
from tests.support.notebooks import Cell, Notebook, write_json
from tests.support.outcomes import find_manifest
from tests.support.runner import run

_SNAPSHOTS: dict = {}


def snapshotted(directory: Path, *cells: Cell, extra: Iterable[Cell] = ()) -> Path:
    """`cells` snapshotted with `--output`, then `extra` appended. The caller registers on the fake
    PyPI whatever the snapshot looks up. Raises RuntimeError, not AssertionError, if snapshot fails,
    so a failed setup can't pass as an expected failure."""
    directory = Path(directory)
    if cells not in _SNAPSHOTS:
        outcome = run("snapshot", Notebook(*cells).write(directory / "snapshot"), "--output")
        if outcome.exit_code != 0:
            raise RuntimeError(f"snapshot failed (exit {outcome.exit_code}):\n{outcome.log}")
        _SNAPSHOTS[cells] = json.loads(outcome.written[0].read_text(encoding="utf-8"))
    data = copy.deepcopy(_SNAPSHOTS[cells])
    added = Notebook(*extra).to_json()["cells"]
    for i, cell in enumerate(added):
        cell["id"] = f"extra-{i}"
    data["cells"] += added
    return write_json(data, directory, "nb_merged.ipynb")


def altered(path: Path, edit: Callable[[dict], None], rehash: bool = True) -> Path:
    """The notebook at `path`, rewritten in place with `edit` applied to its manifest literal and
    the hash recomputed through models, as a manifest another tool version wrote would be.
    rehash=False keeps the stored hash, as a hand edit would."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    cell, node = find_manifest(data)
    literal = ast.literal_eval(node.value)
    edit(literal)
    if rehash:
        literal["dependency_hash"] = SteadyPyManifest.from_literal(literal).verified_hash
    lines = "".join(cell["source"]).splitlines(keepends=True)
    lines[node.lineno - 1:node.end_lineno] = [f"STEADY_PY_MANIFEST = {literal!r}\n"]
    cell["source"] = lines
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return path
