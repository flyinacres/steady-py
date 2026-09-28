"""F7's kernel runs on the venv's interpreter, in the given directory, and the harness's own call
never becomes part of the session it scans."""
import pytest

from tests.support.kernel import kernel, run_live
from tests.support.notebooks import code

pytestmark = pytest.mark.kernel


def test_kernel_runs_the_venvs_interpreter_in_the_given_directory(tmp_path, live_venv):
    with kernel(live_venv, tmp_path) as k:
        out, err = k.execute("import os, sys\nprint(sys.executable)\nprint(os.getcwd())")
    assert err == ""
    executable, cwd = out.splitlines()
    assert executable.startswith(str(live_venv.path)) and cwd == str(tmp_path)


def test_live_scan_sees_the_cells_but_not_the_harness_call(tmp_path, live_venv):
    outcome = run_live(live_venv, tmp_path, [code("x = 1"), code("raise ValueError('shown in the log')")])
    assert outcome.exit_code in (0, 1)
    assert "shown in the log" in outcome.log
    assert outcome.dependencies() == []  # the harness imports steady_py, outside the session history
