"""F2: run the CLI in-process (run) or in a venv (run_in), returning the same Outcome."""
import contextlib
import io
import json
import logging
import os
import subprocess
from typing import Optional

from steady_py import cli
from tests.support.envs import Venv
from tests.support.outcomes import Outcome


class _Capture(logging.StreamHandler):
    """A distinct type, so the CLI still installs its own stderr handler exactly as in real use."""


def run(*argv) -> Outcome:
    """`steady-py <argv>` in this process. Adds `--format json` unless a format is given. Log output
    is captured from the steady_py logger, not stderr: configure_console binds sys.stderr on the
    first call. The logger's level, handlers and propagation are restored, since --quiet/--verbose
    and configure_console persist for the life of the process."""
    argv = _json_by_default(argv)
    logger = logging.getLogger("steady_py")
    saved = logger.level, list(logger.handlers), logger.propagate
    log, out = io.StringIO(), io.StringIO()
    capture = _Capture(log)
    capture.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(capture)
    try:
        with contextlib.redirect_stdout(out):
            try:
                cli.main(argv)
                code = 0
            except SystemExit as exit:
                code = exit.code if isinstance(exit.code, int) else int(exit.code is not None)
    finally:
        logger.setLevel(saved[0])
        logger.handlers[:] = saved[1]
        logger.propagate = saved[2]
    return Outcome(code, out.getvalue(), log.getvalue(), _report(argv, out.getvalue()))


def run_in(venv: Venv, *argv, env: Optional[dict] = None) -> Outcome:
    """The same run as a subprocess of `venv`'s interpreter, returning the same Outcome; the log is
    its stderr. `env` entries (such as sites.pythonpath(...)) override the inherited environment;
    PYTHONPATH is always replaced, never inherited."""
    argv = _json_by_default(argv)
    full_env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    result = subprocess.run([str(venv.python), "-m", "steady_py", *argv], capture_output=True,
                            text=True, encoding="utf-8", env={**full_env, **(env or {})})
    return Outcome(result.returncode, result.stdout, result.stderr, _report(argv, result.stdout))


def _json_by_default(argv) -> list:
    argv = [str(a) for a in argv]
    return argv if "--format" in argv else argv + ["--format", "json"]


def _report(argv: list, stdout: str) -> Optional[dict]:
    if argv[argv.index("--format") + 1] != "json":
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None
