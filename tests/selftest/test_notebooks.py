"""F1 writes files Jupyter accepts and keeps each cell's text, type, order and count exactly."""
import json

import pytest

from tests.support.notebooks import Notebook, code, md


@pytest.mark.filterwarnings("error")  # nbformat repairs some defects (missing IDs) with only a warning
def test_written_notebooks_are_valid_nbformat(tmp_path):
    nbformat = pytest.importorskip("nbformat")
    for i, metadata in enumerate((None, {})):
        path = Notebook(md("# T"), code("import os", 1), metadata=metadata).write(tmp_path, f"{i}.ipynb")
        nbformat.validate(json.loads(path.read_bytes()))


def test_cells_round_trip_in_notebook_order(tmp_path):
    cells = (
        md("# Setup"),
        code("""
            if True:
                x = 1  # é
            """, execution_count=2),
        code("\n\n%%writefile t.py\nimport torch"),  # leading blank line kept (G13)
        code("s = '''\n%end'''\n"),
    )
    data = json.loads(Notebook(*cells).write(tmp_path).read_bytes())
    assert [(c["cell_type"], "".join(c["source"]), c.get("execution_count")) for c in data["cells"]] == [
        ("markdown", "# Setup", None),
        ("code", "if True:\n    x = 1  # é", 2),
        ("code", "\n%%writefile t.py\nimport torch", None),
        ("code", "s = '''\n%end'''", None),
    ]
    assert data["cells"][1]["source"] == ["if True:\n", "    x = 1  # é"]
