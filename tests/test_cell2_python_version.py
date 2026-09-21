"""Generated Cell 2 warns when the notebook runs on a different Python than it was made with, and
then carries on: pip gives its own clear error for a pin that cannot be installed, so stopping
early would only hide the other problems."""
import os
import subprocess
import sys

import pytest

import steady_py.core as spy

CURRENT = (sys.version_info.major, sys.version_info.minor)


def _run_cell2(tmp_path, required):
    """Runs the generated Cell 2 (no pins, so nothing is installed) as if it had been made with `required`."""
    code = spy.generate_production_blueprint([])["step2_code"]
    assert f"REQUIRED_PYTHON = {CURRENT}" in code
    path = tmp_path / "cell2.py"
    path.write_text(code.replace(f"REQUIRED_PYTHON = {CURRENT}", f"REQUIRED_PYTHON = {required}"), encoding="utf-8")
    # Cell 2 prints emoji. A notebook kernel is UTF-8; a bare subprocess on Windows is not, so say so.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, str(path)], capture_output=True, text=True, encoding="utf-8", env=env)


def test_a_matching_python_prints_no_warning(tmp_path):
    result = _run_cell2(tmp_path, CURRENT)
    assert result.returncode == 0 and "created with Python" not in result.stdout


@pytest.mark.parametrize("required", [(CURRENT[0], CURRENT[1] + 1), (2, 7), (4, CURRENT[1]), (2, CURRENT[1])])
def test_any_other_python_warns_and_carries_on(tmp_path, required):
    result = _run_cell2(tmp_path, required)
    label = f"{required[0]}.{required[1]}"
    assert f"This code was created with Python {label}. You are trying to run it with {CURRENT[0]}.{CURRENT[1]}." in result.stdout
    assert f"consider changing your runtime Python version back to {label}" in result.stdout
    assert result.returncode == 0
    assert "Setup complete!" in result.stdout   # it went on to do the installs


def test_the_template_no_longer_contains_a_hard_stop():
    code = spy.generate_production_blueprint([])["step2_code"]
    assert "sys.exit(" not in code.split("STEADY_PY_MANIFEST")[0]
    assert "hard stop" not in code