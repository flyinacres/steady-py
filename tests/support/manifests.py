"""Notebooks that carry a manifest, made by a real snapshot (design §4.5). The snapshot of a given
set of cells runs once per session; each caller gets a copy with its own cells appended, as a
creator's later edits would be."""
import copy
import json
from pathlib import Path
from typing import Iterable

from tests.support.notebooks import Cell, Notebook, write_json
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
