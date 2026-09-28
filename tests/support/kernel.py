"""F7: a real Jupyter kernel on a test venv, driven through jupyter_client. Cells run as a user
types them (magics included), so the live path reads the session history IPython really keeps."""
import json
import os
import queue
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Tuple

from tests.support.envs import Venv
from tests.support.notebooks import Cell
from tests.support.outcomes import Outcome

_MARK = "STEADY_PY_TEST_LIVE_REPORT "
# The call a user makes in a cell, run with store_history=False so it isn't part of the session
# it scans. The JSON is the CLI's own, so the Outcome accessors apply unchanged.
_CALLS = {
    "scan": "_r = _endpoints.scan()\n_code, _json = _cli.scan_exit_code(_r), _cli.format_scan_result(_r)",
    "snapshot": "_r = _endpoints.snapshot()\n_code, _json = _cli.snapshot_exit_code(_r), _cli.format_snapshot_result(_r, 'json')",
}
_IMPORTS = "import json as _json_mod\nfrom steady_py import cli as _cli, endpoints as _endpoints\n"
_PRINT = f"\nprint({_MARK!r} + _json_mod.dumps({{'exit': _code, 'report': _json_mod.loads(_json)}}))"


class Kernel:
    def __init__(self, client):
        self._client = client

    def execute(self, code: str, store_history: bool = True, timeout: float = 120) -> Tuple[str, str]:
        """Runs `code` as one cell; returns (stdout, stderr and error tracebacks)."""
        msg_id = self._client.execute(code, store_history=store_history)
        out: List[str] = []
        err: List[str] = []
        while True:
            try:
                msg = self._client.get_iopub_msg(timeout=timeout)
            except queue.Empty:
                raise RuntimeError(f"kernel timed out after {timeout}s running:\n{code}")
            if msg["parent_header"].get("msg_id") != msg_id:
                continue
            kind, content = msg["msg_type"], msg["content"]
            if kind == "stream":
                (out if content["name"] == "stdout" else err).append(content["text"])
            elif kind == "error":
                err.append("\n".join(content["traceback"]))
            elif kind == "status" and content["execution_state"] == "idle":
                return "".join(out), "".join(err)


@contextmanager
def kernel(venv: Venv, cwd: Path, env: Optional[dict] = None) -> Iterator[Kernel]:
    """A kernel on `venv`'s interpreter, started in `cwd` (the live path's notebook directory).
    pip in the kernel is offline (PIP_NO_INDEX), so install lines resolve against what's installed."""
    from jupyter_client import KernelManager
    from jupyter_client.kernelspec import KernelSpecManager

    specs = Path(cwd).parent / f"{Path(cwd).name}-kernelspec"
    (specs / "steady-py-test").mkdir(parents=True, exist_ok=True)
    (specs / "steady-py-test" / "kernel.json").write_text(json.dumps({
        "argv": [str(venv.python), "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        "display_name": "steady-py test", "language": "python"}), encoding="utf-8")
    full_env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    manager = KernelManager(kernel_name="steady-py-test", kernel_spec_manager=KernelSpecManager(kernel_dirs=[str(specs)]))
    manager.start_kernel(cwd=str(cwd), env={**full_env, "PIP_NO_INDEX": "1", **(env or {})})
    client = manager.client()
    client.start_channels()
    try:
        client.wait_for_ready(timeout=60)
        yield Kernel(client)
    finally:
        client.stop_channels()
        manager.shutdown_kernel(now=True)


def run_live(venv: Venv, cwd: Path, cells: Iterable[Cell], verb: str = "scan", env: Optional[dict] = None) -> Outcome:
    """Runs `cells` in a fresh kernel, then the live `verb` (scan or snapshot of the session), and
    returns the same Outcome the CLI would. Everything the kernel printed to stderr, including
    cell errors, is the log."""
    with kernel(venv, cwd, env) as k:
        logs = [k.execute(cell.source)[1] for cell in cells if cell.cell_type == "code"]
        stdout, stderr = k.execute(_IMPORTS + _CALLS[verb] + _PRINT, store_history=False)
    log = "\n".join([*logs, stderr])
    marked = [line for line in stdout.splitlines() if line.startswith(_MARK)]
    if not marked:
        raise RuntimeError(f"the live {verb} printed no report:\n{stdout}\n{log}")
    result = json.loads(marked[-1][len(_MARK):])
    return Outcome(result["exit"], json.dumps(result["report"]), log, result["report"])
