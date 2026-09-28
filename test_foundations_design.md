# Test foundations design

Scope: the shared support code that the tests in `test_triage_matrix.md` are built on. This is design only; code follows once the design settles, and the design is revised wherever the code proves it wrong.

## 1. Principles

1. Tests are code. One owner per concern, as in the product: one notebook builder, one way to run the tool, one way to read its output, one fake PyPI. Evidence the current suite needs this: 15 notebook-writing helpers across 8 test files, and an `offline` fixture defined three times (`test_endpoints.py`, twice in `test_cli.py`).
2. Assert outcomes at stable boundaries: exit code, the `--format json` report, the manifest literal in a written notebook, Cell 2's printed output. These survive the rearchitecture; internal function results don't.
3. Coupling to internals lives in `tests/support/` only. New test files import `tests.support` and the public API (`steady_py.scan/snapshot/check/install`, the CLI), never `steady_py.<module>`. A hygiene test enforces this for the new test directory.
4. Fake only external boundaries: the network (fake PyPI), and hardware (the existing fake GPU packages). The installed environment, the filesystem and the kernel are real. No monkeypatching of steady-py in new tests.
5. Known bugs are strict xfails that must fail for the right reason: `raises=AssertionError`, so a harness crash (TypeError, missing fixture) is a real failure, not an expected one. The sabotage rule still applies when a bug is fixed and its xfail flips.
6. Every matrix row maps to tests through a marker, so coverage of the matrix can be listed.

## 2. Layout

1. `tests/support/`: `notebooks.py` (F1), `outcomes.py` and `runner.py` (F2), `fake_pypi.py` (F3), `sites.py` (F4), `envs.py` (F5 venvs and wheelhouse), `gitrepo.py` (F6), `kernel.py` (F7).
2. `tests/conftest.py`: markers, the shared fixtures, and the `known_bug` helper.
3. `tests/behavior/`: new tests grouped by product behavior, not by finding, so the files stay meaningful after the bugs are fixed: `test_reading.py`, `test_install_lines.py`, `test_environment.py`, `test_editables.py`, `test_pins.py`, `test_validation.py`, `test_runtime.py`, `test_output.py`.
4. `tests/fixtures/`: saved artifacts. `notebooks/` (real-world patterns worth reading), `projects/` (stub package sources wheels are built from), `pypi/` (trimmed real PyPI responses shared by several tests).
5. `tests/.wheelhouse/` (gitignored): downloaded build backends and built wheels.
6. Existing test files stay in place until the pruning step.

## 3. Markers and tiers

1. `venv`: runs the tool in a real venv (matrix layer V). `kernel`: a real kernel (L). `docker`: container scenarios (D).
2. Default `pytest` deselects `venv`, `kernel` and `docker`, so the fast loop after each sub-step stays fast. `pytest -m venv` (and so on) runs a tier; `pytest -m "venv or kernel"` before a delivery.
3. A tier whose prerequisites are missing (empty wheelhouse with no network, no git, no Docker) skips with a reason naming the missing piece; it never fails for that.
4. `finding(id)`: attaches a matrix ID. `known_bug(id, why)` is the single decorator for a known bug: it applies `finding(id)` plus `xfail(strict=True, raises=AssertionError, reason=f"{id}: {why}")`. A small conftest hook, `--findings`, lists matrix IDs with their tests and status.

## 4. F1 Notebook builder

1. A `Notebook` value: an ordered list of cells built with `md(text)` and `code(text, execution_count=None)`; `code` dedents its text so tests read naturally. Notebook metadata defaults to a Python kernelspec, with options for none (Kaggle HTML reconstructions) and for another language.
2. `write(directory, name="nb.ipynb") -> Path` writes nbformat 4.5 JSON with cell IDs. Options for CRLF line endings and non-UTF-8 residue belong here, since they are properties of the file.
3. The same `Notebook` value feeds the kernel runner (F7), so one scenario runs in file mode and in live mode. That gives file-versus-live parity checks for free (G15, K12).
4. Manifest-carrying notebooks come from running a real `snapshot` on a builder notebook. Scenarios that need an altered manifest (old baseline, tampered hash) use one support function that edits the literal and, when wanted, recomputes the hash through `models`: the one place tests touch manifest internals.
5. Saved notebooks: real-world patterns used by several tests or worth a human reading (a Colab-guarded install, a cookbook-style multi-install cell, a conda cell). `kitchen_sink.ipynb` and `magic_sink.ipynb` stay. Single-test scenarios are built inline, so test and data sit together.

## 5. F2 Runner and outcomes

1. One `Outcome` type for every way of running the tool: `exit_code`, `report` (parsed JSON or None), `log` (text), `written` (paths).
2. `run(*argv)` runs in-process through `cli.main(argv)` and catches `SystemExit`. It captures stdout with `redirect_stdout`, and log output with a handler attached to the `steady_py` logger for the call, not stderr: `configure_console` binds `sys.stderr` on its first call and keeps it, so a later test's stderr capture would miss messages. It restores the logger's level and handlers afterward, because `--quiet` and `--verbose` set the level for the life of the process.
3. `run_in(venv, *argv)` runs the same arguments as a subprocess of a venv's interpreter and returns the same `Outcome`, so a test can move between layers without changing its assertions.
4. Accessors over the report, the only code that knows its shape: `pins()` (canonical name to specifier), `dependency(name)` (status, flags), `raw_installs()`, `warnings(type=None, cell=None)`, `notices(...)`, `delta()`. Diagnostics match on `DiagnosticEvent.type`, never on message text.
5. `manifest(path)` reads a written notebook with its own per-cell `ast` search for the `STEADY_PY_MANIFEST` assignment and `ast.literal_eval`. It deliberately doesn't use steady-py's extractor, which has its own bug (K1); an oracle must not share the code under test.
6. Rendered text is asserted only where the text is the behavior (E8, D6, runtime summaries), through `cell2_text()` and `install_output()` accessors, with a short comment in each test saying why text is the contract.

## 6. F3 Fake PyPI

1. A threaded `http.server` on `127.0.0.1`, one per session on an ephemeral port. The `pypi` fixture clears its registry per test and sets `STEADY_PY_PYPI_URL`, so it works in-process and in subprocesses (the V tier passes the environment through).
2. Registry API: `pypi.add(name, releases={version: {...}})`, where each release takes `requires_dist`, `requires_python`, `yanked`, `upload_time`. The fake builds PyPI-shaped JSON for `/pypi/<name>/json` and `/pypi/<name>/<version>/json` with only the fields steady-py reads, and normalizes the path name per PEP 503.
3. Failure injection per project or version: 404, HTTP 500, and a connection closed mid-body (K3's `IncompleteRead` and `RemoteDisconnected`). A true timeout is left out: the client timeout is 10 seconds and not configurable, and the closed-connection case exercises the same handler.
4. Strict by default: a lookup of an unregistered project returns 404 and is recorded, and the fixture fails the test at teardown unless the test declared `pypi.allow_unknown()`. A silent 404 would otherwise masquerade as "not on PyPI".
5. Realistic graphs (torch's platform-marked `requires_dist` for K5) load from trimmed real responses in `tests/fixtures/pypi/`, captured once by a small script and reduced to the fields used.
6. Later: the PEP 691 simple API, when index lookups for custom-index pins are built (CI1).
7. This replaces the three `offline` fixtures and the `urlopen` patch in `test_drift_check.py` when those files are migrated.

## 7. F4 Site directories and dist-infos

1. `SiteDir(path).add(name, version, ...)` writes `<name>-<version>.dist-info/` with `METADATA` (name, version, `Requires-Dist`, `Provides-Extra`), `INSTALLER`, `RECORD`, `top_level.txt`, and optional `direct_url.json`, plus the module files it lists. `RECORD` entries need no hashes; neither `importlib.metadata` nor `pip freeze` checks them.
2. Recurring shapes are plain functions, not subclasses: `conda_stub` (INSTALLER `conda`, METADATA only, as conda-forge's OpenCV recipe writes), `vcs_ref(commit, subdirectory)`, `file_url(path)`, `editable(source_dir)` (`dir_info.editable` plus a `.pth`).
3. Versions are written verbatim, so legacy versions (E6) and local tags (`2.6.0+cu124`) need no build tool.
4. Consumption is subprocess-only. The tool reads its own interpreter, and it runs `pip freeze` in a subprocess that sees `PYTHONPATH` but not in-process `sys.path` edits, so in-process use would give inconsistent environments. Site dirs are placed on `PYTHONPATH` of a `run_in` call, in order, which also produces shadowing (E3, K6) without a second venv.
5. The writer has self-tests with an independent oracle: `pip freeze` and `importlib.metadata` in the base venv must both report each written shape as intended.

## 8. F5 Venvs and wheelhouse

1. Base venv: session-scoped, `python -m venv`, with steady-py installed from a wheel built once per session. Non-editable on purpose, so steady-py itself doesn't appear as an editable in the environment it inspects. It contains only pip, steady-py and its dependencies, so it is deterministic.
2. `venv.python` abstracts `bin/python` versus `Scripts\python.exe`.
3. Variants: `without_pip` (E1: freeze has nothing to run), and fresh per-test venvs for tests that need real installs (editables, R1 to R3, D6, E2a, E5). Those cost a few seconds each; about 12 tests.
4. Wheelhouse contents:
   1. Build backends (setuptools, wheel, hatchling, pdm-backend, editables), downloaded once with `pip download`. The tier skips when they're absent and there's no network.
   2. Stub wheels built per session from `tests/fixtures/projects/`: `demo-extras` (an extra, R1), `demo-a` and `demo-b` (installing B moves A, R2), `demo-loc` at `1.0+cu126` (R3), `demo-broken` (D6), an in-tree project depending on `demo-a` (ED1, avoiding a network dependency), a hatchling fork providing a renamed module (ED4), a pdm project, and a monorepo with `alpha` and `beta` (E5, via F6). The existing `notebook_env_test_fixture` package folds in here under a new name.
5. Installs run with `PIP_NO_INDEX=1` and `PIP_FIND_LINKS=<wheelhouse>`, the mechanism development.md already documents for Cell 2. Editable builds keep build isolation, which also resolves backends from find-links offline, so backends never land in the test venv.

## 9. F6 Local git repositories

1. A fixture copies a saved project into tmp, commits it, and returns a `git+file://...@<commit>` reference, with `#subdirectory=` for the monorepo.
2. Skips when git is missing.

## 10. F7 Kernel runner

1. The kernel manager moves from `tests/runners/e2e_harness.py` into `tests/support/kernel.py`; the runners import it rather than keep a second copy.
2. The kernel runs in a kernel venv: the base venv plus ipykernel from the wheelhouse.
3. `run_live(notebook, call)` executes the builder notebook's code cells in order through `jupyter_client`, then runs the steady-py call and prints its JSON between markers, parsed into the same `Outcome`. Kernel-side errors land in `Outcome.log`.
4. A parity helper compares a file-mode and a live-mode `Outcome` on chosen fields (pins, raw installs, warning types).

## 11. F8 Docker scenarios

1. The runners become pytest-collected tests marked `docker`, each invoking `docker run`, so results are per test and xfail works. `run_suite.py` becomes a thin driver passing the tier to pytest.
2. New scenarios: a non-root user with a root-owned Python and venv (R4, R5, D2), and a Miniconda image for a one-time confirmation of the conda rows (E2b, ED4's `conda develop`, C1).
3. Detailed design is deferred to roadmap step 3.

## 12. Production changes made

1. `pypi.py`: the PyPI base URL comes from `STEADY_PY_PYPI_URL` (default `https://pypi.org`), read on every call. It's also mirror support; README and HELP don't mention it yet.
2. `cli.py`: `main(argv=None)` passes `argv` to `parse_args`, so the in-process runner doesn't patch `sys.argv`. Behavior is otherwise unchanged; the console script and `__main__` call it with no arguments.
3. Verified: `test_drift_check.py` and `test_cli.py` pass (223 tests) on a locally rebuilt `src/` layout.

## 13. To verify while coding

1. Build isolation resolves hatchling and pdm-backend from find-links offline, on Windows and Linux.
2. `git+file://` URL form for Windows drive paths.
3. `DiagnosticEvent.to_dict` omits `level`; warnings and notices are separate lists, so matching by `type` within the list should suffice. Confirm no two diagnostics share a `type` with different meanings.
4. Whether per-session base-venv creation is fast enough, or should be cached across sessions keyed on a hash of `src/` and `pyproject.toml`.
5. The ANSI code-page case in E1 runs only on the Windows host.
