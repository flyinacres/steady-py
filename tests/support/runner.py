"""F2: run the CLI in-process and return an Outcome."""
import contextlib
import io
import json
import logging

from steady_py import cli
from tests.support.outcomes import Outcome


class _Capture(logging.StreamHandler):
    """A distinct type, so the CLI still installs its own stderr handler exactly as in real use."""


def run(*argv) -> Outcome:
    """`steady-py <argv>` in this process. Adds `--format json` unless a format is given. Log output
    is captured from the steady_py logger, not stderr: configure_console binds sys.stderr on the
    first call. The logger's level and handlers are restored, since --quiet/--verbose persist."""
    argv = [str(a) for a in argv]
    if "--format" not in argv:
        argv += ["--format", "json"]
    logger = logging.getLogger("steady_py")
    saved = logger.level, list(logger.handlers)
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
    try:
        report = json.loads(out.getvalue()) if argv[argv.index("--format") + 1] == "json" else None
    except json.JSONDecodeError:
        report = None
    return Outcome(code, out.getvalue(), log.getvalue(), report)
