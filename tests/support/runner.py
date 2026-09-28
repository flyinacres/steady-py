"""F2: run the CLI in-process (run) or in a venv (run_in), and Cell 2's installer in a venv
(install_in), each returning an Outcome."""
import contextlib
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional, Tuple

from steady_py import cli
from tests.support.envs import Venv
from tests.support.outcomes import Outcome, cell2_text


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


# What Cell 2 runs after its bootstrap, with the result printed on a marked line.
_RESULT_MARK = "STEADY_PY_TEST_INSTALL_RESULT "
INSTALL_SCRIPT = ("import dataclasses, json, sys, steady_py\n"
                  "with open(sys.argv[1], encoding='utf-8') as f:\n"
                  "    result = steady_py.install(json.load(f))\n"
                  f"print({_RESULT_MARK!r} + json.dumps(dataclasses.asdict(result)))\n")


def write_install_inputs(directory: Path, pins: Iterable[str], python: Tuple[int, int] = sys.version_info[:2],
                         raw_installs: Iterable[str] = ()) -> Path:
    """Writes install.py and manifest.json for `pins` ("name==version", extras allowed) and
    `raw_installs` (verbatim) into `directory`; returns the script. The manifest holds only the
    fields install() reads."""
    deps = [dict(zip(("name", "version"), pin.split("==", 1)), flags=[]) for pin in pins]
    manifest = {"python_version": {"major": python[0], "minor": python[1]}, "dependencies": deps,
                "raw_installs": list(raw_installs)}
    (Path(directory) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    script = Path(directory) / "install.py"
    script.write_text(INSTALL_SCRIPT, encoding="utf-8")
    return script


def _from_wheels_only(wheels: Path) -> dict:
    """The environment for pip to install from `wheels` alone, as Cell 2's runner would configure it."""
    return {**os.environ, "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(wheels)}


def install_in(venv: Venv, pins: Iterable[str], wheels: Path, *, raw_installs: Iterable[str] = (),
               python: Tuple[int, int] = sys.version_info[:2]) -> Outcome:
    """steady_py.install() for `pins` and `raw_installs` in `venv`, installing from `wheels` only,
    as Cell 2 does with pip's own PIP_NO_INDEX/PIP_FIND_LINKS. `python` is the manifest's target."""
    script = write_install_inputs(Path(wheels).parent, pins, python, raw_installs)
    result = subprocess.run([str(venv.python), str(script), str(script.parent / "manifest.json")],
                            capture_output=True, text=True, encoding="utf-8", env=_from_wheels_only(wheels))
    return install_outcome(result.returncode, result.stdout, result.stderr)


def run_cell2(venv: Venv, notebook: Path, wheels: Path) -> Outcome:
    """The setup code cell of `notebook`, unmodified, run as a script in `venv`, installing from
    `wheels` only. There is no report: Cell 2 prints for a person, not a program."""
    script = Path(wheels).parent / "cell2.py"
    script.write_text(cell2_text(notebook), encoding="utf-8")
    result = subprocess.run([str(venv.python), str(script)], capture_output=True, text=True,
                            encoding="utf-8", env=_from_wheels_only(wheels))
    return Outcome(result.returncode, result.stdout, result.stderr, None)


def install_outcome(returncode: int, stdout: str, stderr: str) -> Outcome:
    """An Outcome for an installer run: stdout is what the runner sees, the report the InstallResult."""
    marked = [line for line in stdout.splitlines() if line.startswith(_RESULT_MARK)]
    report = json.loads(marked[-1][len(_RESULT_MARK):]) if marked else None
    printed = "\n".join(line for line in stdout.splitlines() if not line.startswith(_RESULT_MARK))
    return Outcome(returncode, printed, stderr, report)
