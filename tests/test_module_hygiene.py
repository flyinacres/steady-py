"""Importing steady_py must not touch process-global state; only the CLI entry point may.
Also pins that best-effort probes stay quiet by default but leave a trace under --verbose."""
import logging
import os
import subprocess
import sys
from pathlib import Path

import steady_py.core as spy

SRC_DIR = str(Path(spy.__file__).resolve().parents[1])


def _run(code: str, **env) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=SRC_DIR,
        env={**os.environ, **env},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_import_leaves_streams_and_logging_untouched():
    out = _run(
        "import sys, logging\n"
        "before = sys.stdout.encoding\n"
        "import steady_py.core as spy\n"
        "log = logging.getLogger('steady_py')\n"
        "print(sys.stdout.encoding == before, [type(h).__name__ for h in log.handlers], log.propagate)",
        PYTHONIOENCODING="latin-1",
    )
    assert out == "True ['NullHandler'] False"


def testconfigure_console_is_where_streams_and_the_handler_get_set_up():
    """The stderr handler replaces the import-time placeholder, leaving exactly one handler."""
    out = _run(
        "import sys, logging, steady_py.core as spy\n"
        "spy.configure_console()\n"
        "spy.configure_console()  # idempotent\n"
        "log = logging.getLogger('steady_py')\n"
        "print(sys.stdout.encoding, sorted(type(h).__name__ for h in log.handlers), log.propagate)",
        PYTHONIOENCODING="latin-1",
    )
    assert out == "utf-8 ['StreamHandler'] False"


def test_failed_opencv_probe_falls_back_and_is_logged_at_debug(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise OSError("pip is unavailable")

    monkeypatch.setattr(spy.subprocess, "run", boom)
    caplog.set_level(logging.DEBUG, logger="steady_py")
    assert spy.resolve_opencv_variant() == "opencv-python"
    assert "Could not inspect installed OpenCV variants" in caplog.text


def test_reloading_the_module_in_one_process_never_stacks_handlers():
    """Reloading the tool in a live kernel (autoreload, or a re-import after an edit) re-runs the whole file."""
    out = _run(
        "import importlib, logging, sys, steady_py.core as spy\n"
        "for _ in range(2):\n"
        "    importlib.reload(spy)\n"
        "    spy.configure_console()\n"
        "print(len(logging.getLogger('steady_py').handlers))"
    )
    assert out == "1"
