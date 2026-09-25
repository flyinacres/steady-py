"""Guards the signal/severity/status constants cleanup: production code must use the named constants,
not bare string literals, for these values (a typo in a literal silently never matches)."""
import re
from pathlib import Path

import steady_py.core as spy
from steady_py import constants, drift, models

PACKAGE_DIR = Path(spy.__file__).resolve().parent
SOURCES = {path.name: path.read_text(encoding="utf-8", errors="replace") for path in sorted(PACKAGE_DIR.glob("*.py"))}
BARE = re.compile(r'\b(signal|severity|baseline_status|kind|status)\b(\s*(?:==|!=|=)\s*|\s+(?:not\s+)?in\s+\(?)"[a-z_]+"')


def _code_lines():
    """Lines of every module outside triple-quoted text (docstrings and the generated notebook cells)."""
    for name, source in SOURCES.items():
        inside = False
        for number, line in enumerate(source.splitlines(), 1):
            quotes = line.count('"""')
            if not inside and quotes == 0:
                yield f"{name}:{number}", line
            if quotes % 2:
                inside = not inside


def test_no_bare_literals_for_signal_severity_or_status():
    offenders = [f"{where}: {line.strip()}" for where, line in _code_lines() if BARE.search(line)]
    assert offenders == []


def test_constant_values_are_the_wire_strings():
    assert constants.Signal.NOT_FOUND_ON_PYPI == "not_found_on_pypi"
    assert constants.Severity.NOTICE == "notice"
    assert constants.BaselineStatus.NOT_CHECKED_AT_GENERATION == "not_checked_at_generation"
    assert constants.DependencyStatus.DIRECT_REFERENCE == "direct_reference"
    assert constants.FetchStatus.NETWORK_ERROR == "network_error"
    assert constants.ReportKind.VALIDATION == "validation"


def test_constants_embed_in_the_manifest_literal_as_plain_strings():
    baseline = drift.build_baseline([models.DriftFinding("requests", "2.32.0", constants.Signal.YANKED, constants.Severity.CONFIRMED, "m")])
    assert repr(baseline.to_dict()) == "{'version': 1, 'findings': [['yanked', 'requests', '2.32.0']], 'errors': []}"


def test_scan_covers_every_module_in_the_package():
    assert {"core.py", "cli.py", "endpoints.py", "results.py", "constants.py", "models.py", "util.py", "installed.py", "pypi.py", "scanning.py", "magics.py", "localmodules.py", "accelerator.py", "resolution.py", "drift.py", "analyze.py"} <= set(SOURCES)
