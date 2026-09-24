"""Cell 2's installer (steady_py.install) warns when the notebook runs on a different Python
than it was made with, and then carries on: pip gives its own clear error for a pin that cannot
be installed, so stopping early would only hide the other problems."""
import sys

import pytest

import steady_py.core as spy

CURRENT = (sys.version_info.major, sys.version_info.minor)


def _manifest(required):
    return {
        "python_version": {"major": required[0], "minor": required[1]},
        "dependencies": [], "raw_installs": [], "custom_sourced": [], "generated_at": "",
    }


def test_a_matching_python_prints_no_warning(capsys):
    spy.install(_manifest(CURRENT))
    assert "created with Python" not in capsys.readouterr().out


@pytest.mark.parametrize("required", [(CURRENT[0], CURRENT[1] + 1), (2, 7), (4, CURRENT[1]), (2, CURRENT[1])])
def test_any_other_python_warns_and_carries_on(required, capsys):
    spy.install(_manifest(required))
    out = capsys.readouterr().out
    label = f"{required[0]}.{required[1]}"
    assert f"This code was created with Python {label}. You are trying to run it with {CURRENT[0]}.{CURRENT[1]}." in out
    assert f"consider changing your runtime Python version back to {label}" in out
    assert "Setup complete!" in out   # it went on (there were no dependencies to install)


def test_the_template_no_longer_contains_a_hard_stop():
    code = spy.generate_production_blueprint([])["step2_code"]
    assert "sys.exit(" not in code
