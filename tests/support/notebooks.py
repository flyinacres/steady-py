"""F1: build notebooks inline, in notebook order, and write them as real nbformat 4.5 files."""
import copy
import json
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PYTHON_METADATA = {
    "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
    "language_info": {"name": "python"},
}


@dataclass(frozen=True)
class Cell:
    cell_type: str
    source: str
    execution_count: Optional[int] = None


def _tidy(text: str) -> str:
    """Dedent, drop the one newline that follows opening triple quotes, and trailing whitespace.
    A cell that must start with blank lines adds them after that first newline."""
    text = textwrap.dedent(text)
    return (text[1:] if text.startswith("\n") else text).rstrip()


def md(text: str) -> Cell:
    return Cell("markdown", _tidy(text))


def code(text: str, execution_count: Optional[int] = None) -> Cell:
    return Cell("code", _tidy(text), execution_count)


class Notebook:
    """Cells in notebook order. metadata=None gives a Python kernelspec; pass {} for none
    (Kaggle HTML reconstructions) or another kernelspec for a different language."""

    def __init__(self, *cells: Cell, metadata: Optional[dict] = None):
        self.cells = cells
        self.metadata = PYTHON_METADATA if metadata is None else metadata

    def to_json(self) -> dict:
        cells = []
        for i, cell in enumerate(self.cells):
            entry = {"cell_type": cell.cell_type, "id": f"cell-{i}", "metadata": {},
                     # Jupyter's on-disk form: split after each "\n" only.
                     "source": [line for line in re.split(r"(?<=\n)", cell.source) if line]}
            if cell.cell_type == "code":
                entry.update(execution_count=cell.execution_count, outputs=[])
            cells.append(entry)
        return {"cells": cells, "metadata": copy.deepcopy(self.metadata), "nbformat": 4, "nbformat_minor": 5}

    def write(self, directory: Path, name: str = "nb.ipynb") -> Path:
        return write_json(self.to_json(), directory, name)


def write_json(data: dict, directory: Path, name: str = "nb.ipynb") -> Path:
    """Writes notebook JSON the way Notebook.write does."""
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" so the file is byte-identical on Windows and Linux.
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    return path
