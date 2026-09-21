"""Guards the signal/severity/status constants cleanup: production code must use the named constants,
not bare string literals, for these values (a typo in a literal silently never matches)."""
import re
from pathlib import Path

import steady_py.core as spy

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
    assert spy.Signal.NOT_FOUND_ON_PYPI == "not_found_on_pypi"
    assert spy.Severity.NOTICE == "notice"
    assert spy.BaselineStatus.NOT_CHECKED_AT_GENERATION == "not_checked_at_generation"
    assert spy.DependencyStatus.DIRECT_REFERENCE == "direct_reference"
    assert spy.FetchStatus.NETWORK_ERROR == "network_error"
    assert spy.ReportKind.VALIDATION == "validation"


def test_constants_embed_in_the_manifest_literal_as_plain_strings():
    baseline = spy.build_baseline([spy.DriftFinding("requests", "2.32.0", spy.Signal.YANKED, spy.Severity.CONFIRMED, "m")])
    assert repr(baseline.to_dict()) == "{'version': 1, 'findings': [['yanked', 'requests', '2.32.0']], 'errors': []}"


def test_scan_covers_every_module_in_the_package():
    assert {"core.py", "cli.py", "endpoints.py", "results.py"} <= set(SOURCES)
