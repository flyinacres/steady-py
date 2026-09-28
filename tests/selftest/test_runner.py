"""F2 runs the tool without leaking logger state, and its manifest oracle is independent and exact."""
import logging

import pytest

from tests.support.notebooks import Notebook, code
from tests.support.outcomes import manifest
from tests.support.runner import run


def test_run_captures_the_report_and_log_and_restores_the_logger(tmp_path):
    logger = logging.getLogger("steady_py")
    before = logger.level, list(logger.handlers)
    path = Notebook(code("import packaging")).write(tmp_path)
    run("scan", path, "--quiet")
    assert (logger.level, logger.handlers) == before
    outcome = run("scan", path)
    assert outcome.exit_code == 0
    assert "packaging" in outcome.pins()
    assert "Analyzing" in outcome.log


def test_manifest_oracle_reads_one_top_level_assignment_per_cell(tmp_path):
    cells = (code("x = !ls"), code("STEADY_PY_MANIFEST = {'a': 1}"), code("def f():\n    STEADY_PY_MANIFEST = {}"))
    assert manifest(Notebook(*cells).write(tmp_path)) == {"a": 1}
    with pytest.raises(LookupError):
        manifest(Notebook(*cells, code("STEADY_PY_MANIFEST = {}")).write(tmp_path))
