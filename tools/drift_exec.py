"""The in-container half of tools/drift_study.py: sets up one notebook's environment, runs the notebook
to its first error, and writes what happened to a JSON file. Run with the Python the image's notebooks
use; it starts with the standard library only and imports nbclient once the environment is set up.

Steps, in order, each ending the run when it fails:
1. Install the requirements file, if given (what a runner following the repository's README does).
2. Install each pin (the repin pass), wheels only unless the package has no wheels at all.
3. Make sure nbclient and ipykernel are importable, then register a kernel for this interpreter.
4. Run the notebook cell by cell, stopping at the first error, timeout or kernel death.
Silent install failures (`!pip` and `%pip` never raise) are recorded from each install cell's output.
Progress: one short line per step and per code cell.
"""
import argparse
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

KERNEL_NAME = "drift-study"
ANSI = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
# A file path inside an installed package, as tracebacks and ImportError messages show it.
SITE_FRAME = re.compile(r"(?:site|dist)-packages[/\\]([A-Za-z0-9_.\-]+)")
INSTALL_CELL = re.compile(r"\b(?:pip3?|conda|mamba|uv)\s+(?:pip\s+)?install\b")
PIP_ERROR = re.compile(r"^(?:ERROR:|error:|\s*×|Could not find a version|No matching distribution)", re.MULTILINE)
PIP_FAILED_NAME = re.compile(
    r"(?:No matching distribution found for|Could not find a version that satisfies the requirement"
    r"|Failed to build|Failed building wheel for|Building wheel for)\s+([A-Za-z0-9_.\-]+)")
TAIL_LINES = 25


@dataclass
class InstallError:
    cell: int  # the cell's 1-based position among all cells
    names: List[str]  # distributions pip named as failing, when it named any
    tail: str


@dataclass
class ErrorInfo:
    cell: int
    ename: str
    evalue: str
    traceback: str
    frames: List[Tuple[str, str]]  # (top-level module, distribution or ""), outermost first, deduplicated
    missing_module: str = ""
    missing_top_installed: bool = False  # a ModuleNotFoundError whose top-level package is installed


@dataclass
class Result:
    python: str
    kernel_python: str
    outcome: str = "completed"  # completed, error, timeout, notebook_timeout, kernel_died, requirements_failed, pin_failed, setup_failed
    requirements_status: str = "none"  # none, ok, failed
    requirements_failed_names: List[str] = field(default_factory=list)
    pin_status: str = "none"  # none, ok, no_wheel, failed
    pin_failed: str = ""
    setup_tail: str = ""  # pip or kernel output when a setup step failed
    cells_total: int = 0
    code_cells: int = 0
    cells_run: int = 0
    error: Optional[ErrorInfo] = None
    install_errors: List[InstallError] = field(default_factory=list)
    installed: Dict[str, str] = field(default_factory=dict)
    duration_s: float = 0.0


def say(text: str) -> None:
    print(f"  [exec] {text}", flush=True)


def tail(text: str, lines: int = TAIL_LINES) -> str:
    return "\n".join(ANSI.sub("", text).strip().splitlines()[-lines:])


def pip(*args: str, timeout: int = 1800) -> Tuple[int, str]:
    cmd = [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check", *args]
    try:
        done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, f"pip timed out after {timeout}s: {' '.join(args)}"
    return done.returncode, done.stdout + done.stderr


def failed_names(pip_output: str) -> List[str]:
    """The distributions pip's output names as the ones it could not install, in order."""
    names: List[str] = []
    for match in PIP_FAILED_NAME.finditer(ANSI.sub("", pip_output)):
        name = re.split(r"[<>=!~\[;]", match.group(1))[0]
        if name and name not in names:
            names.append(name)
    return names


def kernel_python() -> str:
    """The interpreter the image's own python3 kernel runs, when it has one: on the hosted images it is
    the environment notebooks see, which need not be the first python3 on PATH. A bare name in the
    kernelspec (ipykernel's default is "python") resolves through PATH, as Jupyter resolves it."""
    try:
        from jupyter_client.kernelspec import KernelSpecManager  # type: ignore[import-not-found, unused-ignore]
        argv0 = str(KernelSpecManager().get_kernel_spec("python3").argv[0])
    except Exception:  # third-party probe: no jupyter_client, or no python3 kernel; use this interpreter
        return sys.executable
    resolved = shutil.which(argv0) or argv0
    return os.path.realpath(resolved)


def installed_versions() -> Dict[str, str]:
    found: Dict[str, str] = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if name and name.lower() not in found:
            found[name.lower()] = dist.version
    return found


def frames_of(text: str) -> List[Tuple[str, str]]:
    """Each installed top-level module a traceback passes through, with the distribution that owns it."""
    owners = importlib.metadata.packages_distributions()
    frames: List[Tuple[str, str]] = []
    for match in SITE_FRAME.finditer(text):
        top = match.group(1).split(".")[0] if not match.group(1).endswith(".py") else match.group(1)[:-3]
        if top.endswith(("-info", "-packages")) or top in {m for m, _ in frames}:
            continue
        frames.append((top, (owners.get(top) or [""])[0]))
    return frames


def error_info(cell: int, ename: str, evalue: str, traceback: str) -> ErrorInfo:
    traceback = ANSI.sub("", traceback)
    info = ErrorInfo(cell=cell, ename=ename, evalue=ANSI.sub("", evalue)[:2000],
                     traceback="\n".join(traceback.splitlines()[-60:]),
                     frames=frames_of(traceback + "\n" + evalue))
    missing = re.search(r"No module named '([^']+)'", evalue)
    if missing:
        info.missing_module = missing.group(1)
        top = info.missing_module.split(".")[0]
        info.missing_top_installed = bool(importlib.metadata.packages_distributions().get(top))
    return info


def cell_text(cell: Any) -> str:
    """The text a cell's outputs show, for spotting pip errors."""
    parts: List[str] = []
    for output in cell.get("outputs", []):
        if output.get("output_type") == "stream":
            parts.append(output.get("text", ""))
        elif output.get("output_type") in {"execute_result", "display_data"}:
            parts.append(str(output.get("data", {}).get("text/plain", "")))
    return "".join(parts)


def setup(args: argparse.Namespace, result: Result) -> bool:
    """Steps 1 to 3. Returns False, with the result's outcome set, when a step failed."""
    if args.requirements:
        say(f"installing {args.requirements}")
        code, out = pip("-r", args.requirements)
        result.requirements_status = "ok" if code == 0 else "failed"
        if code != 0:
            result.outcome, result.setup_tail = "requirements_failed", tail(out)
            result.requirements_failed_names = failed_names(out)
            return False
    constraints = ["-c", args.constraints] if args.constraints else []
    for spec in args.pin:
        name = re.split(r"[<>=!~\[;]", spec)[0]
        binary = [] if name in args.sdist_ok else ["--only-binary=:all:"]
        say(f"pinning {spec}{' with constraints' if constraints else ''}")
        code, out = pip(*binary, spec, *constraints)
        if code != 0:
            no_wheel = bool(binary) and "No matching distribution" in out
            result.outcome, result.pin_status, result.pin_failed = "pin_failed", "no_wheel" if no_wheel else "failed", spec
            result.setup_tail = tail(out)
            return False
        result.pin_status = "ok"
    missing = [m for m in ("nbclient", "nbformat", "ipykernel") if importlib.util.find_spec(m) is None]
    if missing:
        say(f"installing the runner's own tools: {', '.join(missing)}")
        code, out = pip(*missing)
        if code != 0:
            result.outcome, result.setup_tail = "setup_failed", tail(out)
            return False
        importlib.invalidate_caches()
    done = subprocess.run([sys.executable, "-m", "ipykernel", "install", "--user", "--name", KERNEL_NAME],
                          capture_output=True, text=True)
    if done.returncode != 0:
        result.outcome, result.setup_tail = "setup_failed", tail(done.stdout + done.stderr)
        return False
    return True


def execute(args: argparse.Namespace, result: Result) -> None:
    """Step 4."""
    import nbformat  # type: ignore[import-not-found, unused-ignore]
    from nbclient import NotebookClient  # type: ignore[import-not-found, unused-ignore]
    from nbclient.exceptions import CellExecutionError, CellTimeoutError, DeadKernelError  # type: ignore[import-not-found, unused-ignore]

    nb = nbformat.read(args.notebook, as_version=4)  # type: ignore[no-untyped-call, unused-ignore]
    result.cells_total = len(nb.cells)
    code_cells = [(i, c) for i, c in enumerate(nb.cells) if c.cell_type == "code"]
    result.code_cells = len(code_cells)
    client = NotebookClient(nb, timeout=args.cell_timeout, kernel_name=KERNEL_NAME, allow_errors=False,
                            resources={"metadata": {"path": os.getcwd()}})
    started = time.monotonic()
    try:
        with client.setup_kernel():
            for n, (index, cell) in enumerate(code_cells, start=1):
                if time.monotonic() - started > args.notebook_timeout:
                    result.outcome = "notebook_timeout"
                    return
                say(f"cell {index + 1} ({n}/{len(code_cells)} code cells)")
                try:
                    client.execute_cell(cell, index)
                except CellExecutionError as exc:
                    result.outcome = "error"
                    result.error = error_info(index + 1, exc.ename, exc.evalue, exc.traceback)
                    return
                except CellTimeoutError:
                    result.outcome = "timeout"
                    result.error = ErrorInfo(cell=index + 1, ename="CellTimeoutError", evalue="", traceback="", frames=[])
                    return
                except DeadKernelError as exc:
                    result.outcome = "kernel_died"
                    result.error = ErrorInfo(cell=index + 1, ename="DeadKernelError", evalue=str(exc)[:2000],
                                             traceback="", frames=[])
                    return
                finally:
                    result.cells_run = n
                if INSTALL_CELL.search(cell.source):
                    text = cell_text(cell)
                    if PIP_ERROR.search(ANSI.sub("", text)):
                        result.install_errors.append(InstallError(index + 1, failed_names(text), tail(text, 12)))
    except DeadKernelError as exc:  # the kernel died while starting, before any cell
        result.outcome, result.setup_tail = "setup_failed", str(exc)[:2000]
    except RuntimeError as exc:  # nbclient's "Kernel didn't respond" and similar start-up failures
        if result.cells_run == 0:
            result.outcome, result.setup_tail = "setup_failed", str(exc)[:2000]
        else:
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notebook", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--requirements", default="")
    parser.add_argument("--pin", action="append", default=[])
    parser.add_argument("--constraints", default="")
    parser.add_argument("--sdist-ok", action="append", default=[], help="a pinned package with no wheels at all")
    parser.add_argument("--cell-timeout", type=int, default=600)
    parser.add_argument("--notebook-timeout", type=int, default=1800)
    args = parser.parse_args()

    target = kernel_python()
    if os.path.realpath(sys.executable) != target and os.environ.get("DRIFT_EXEC_REEXEC") != "1":
        say(f"switching to the image's kernel interpreter {target}")
        os.environ["DRIFT_EXEC_REEXEC"] = "1"
        os.execv(target, [target, *sys.argv])

    started = time.monotonic()
    result = Result(python=sys.version.split()[0], kernel_python=sys.executable)
    if setup(args, result):
        result.installed = installed_versions()
        execute(args, result)
    result.duration_s = round(time.monotonic() - started, 1)
    Path(args.out).write_text(json.dumps(asdict(result)), encoding="utf-8")
    where = f" at cell {result.error.cell}: {result.error.ename}" if result.error else ""
    say(f"done: {result.outcome}{where} ({result.duration_s}s)")


if __name__ == "__main__":
    main()
