# Test foundations design

Scope: the shared support code that the tests in `test_triage_matrix.md` are built on. The design is revised wherever the code proves it wrong; items marked deferred are built when the first matrix row needs them.

## 1. Principles

1. Tests are code. One owner per concern, as in the product: one notebook builder, one way to run the tool, one way to read its output, one fake PyPI. Evidence the current suite needs this: 15 notebook-writing helpers across 8 test files, and an `offline` fixture defined three times (`test_endpoints.py`, twice in `test_cli.py`).
2. Assert outcomes at stable boundaries: exit code, the `--format json` report, the manifest literal in a written notebook, Cell 2's printed output. These survive the rearchitecture; internal function results don't.
3. Coupling to internals lives in `tests/support/` only. New test files import `tests.support` and the public API (`steady_py.scan/snapshot/check/install`, the CLI), never `steady_py.<module>`. A hygiene test enforces this for `tests/behavior/` and `tests/characterization/`: it rejects submodule imports, non-public names, `steady_py.<internal>` attribute access and `"steady_py.*"` strings (patch targets).
4. Fake only external boundaries: the network (fake PyPI), and hardware (the existing fake GPU packages). The installed environment, the filesystem and the kernel are real. No monkeypatching of steady-py in new tests.
5. Known bugs are strict xfails that must fail for the right reason: `raises=AssertionError`, so a harness crash (TypeError, missing fixture) is a real failure, not an expected one. For the same reason, setup inside a known-bug test (such as the snapshot that produces a manifest) raises `RuntimeError` when it fails, and support accessors raise `LookupError` or `ValueError` on misuse. The sabotage rule still applies when a bug is fixed and its xfail flips.
6. Every matrix row maps to tests through a marker, so coverage of the matrix can be listed.

## 2. Layout

1. `tests/support/`: `markers.py` (`finding`, `known_bug`), `notebooks.py` (F1), `manifests.py` (manifest-carrying notebooks, §4.5), `outcomes.py` and `runner.py` (F2), `fake_pypi.py` (F3), `sites.py` (F4), `envs.py` (F5 venvs and wheelhouse), `gitrepo.py` (F6), `kernel.py` (F7).
2. `tests/__init__.py` makes `tests` a package, so `tests.support` imports under pytest's default import mode.
3. `tests/conftest.py`: the tier markers and their deselection hook, the `--findings` listing, and the shared fixtures (`pypi`, `base_venv`). Shared fixtures and hooks live only here.
4. `tests/behavior/`: new tests grouped by product behavior, not by finding, so the files stay meaningful after the bugs are fixed: `test_reading.py`, `test_install_lines.py`, `test_environment.py`, `test_editables.py`, `test_pins.py`, `test_validation.py`, `test_runtime.py`, `test_output.py`. Its `conftest.py` holds one directory-wide policy: every behavior test runs against the strict fake PyPI (autouse), so no behavior test reaches the real one.
5. `tests/characterization/`: the regression net for the rearchitecture. Plain passing tests of current behavior at the CLI/JSON, public-API and Cell 2 boundaries, grouped by behavior: manifest lifecycle, check signals, scan delta, directory runs, local modules, distribution names, setup runtime. They carry no matrix IDs, so `--findings` doesn't list them. Its `conftest.py` reuses `tests/behavior`'s autouse strict-PyPI fixture, and the public-API hygiene check covers both directories.
6. `tests/selftest/`: tests of the support code itself (markers, hygiene, notebook builder, runner, fake PyPI, site dirs, venvs).
7. `tests/fixtures/`: saved artifacts. `notebooks/` (real-world patterns worth reading), `projects/` (stub package sources wheels are built from), `pypi/` (trimmed real PyPI responses shared by several tests).
8. `tests/.wheelhouse/` (gitignored): downloaded build backends and built wheels.
9. Existing test files stay in place until the pruning step; §14 maps each to its replacement.

## 3. Markers and tiers

1. `venv`: runs the tool in a real venv (matrix layer V). `kernel`: a real kernel (L). `docker`: container scenarios (D).
2. When no `-m` is given, `pytest` deselects `venv`, `kernel` and `docker`, so the fast loop after each sub-step stays fast. `pytest -m venv` (and so on) runs a tier; `pytest -m "venv or kernel"` before a delivery. A tier test named only by node ID is still deselected; add `-m`.
3. A tier whose prerequisites are missing (empty wheelhouse with no network, no git, no Docker) skips with a reason naming the missing piece; it never fails for that. Deferred until the wheelhouse exists: today a failed base-venv build is an error.
4. `finding(id)` attaches a matrix ID. `known_bug(id, why)` is the single helper for a known bug: `finding(id)` plus `xfail(strict=True, raises=AssertionError, reason=f"{id}: {why}")`. It returns a list of marks that also works as a decorator, so it serves `@known_bug(...)`, `pytest.param(..., marks=known_bug(...))` for one parametrized case, and `pytestmark`.
5. `pytest --findings` lists each matrix ID with its tests (status `known bug` or `covered`, tier, node ID), sorted naturally (K2 before K10), including deselected tiers, and runs nothing. How to read it is in the matrix legend.
6. Any `-m` expression turns off the default deselection, so `pytest -m "venv or not venv" tests/characterization` runs one directory across the unit and venv tiers.

## 4. F1 Notebook builder

1. `Notebook(*cells, metadata=None)`, with cells from `md(text)` and `code(text, execution_count=None)`. Both dedent their text and drop only the newline that follows opening triple quotes, plus trailing whitespace; a cell that must start with blank lines (G13) adds them after that newline. `metadata=None` gives a Python kernelspec; `{}` gives none (Kaggle HTML reconstructions); any other dict is used as given (another language).
2. `Notebook.write(directory, name="nb.ipynb") -> Path` writes nbformat 4.5 JSON: deterministic cell IDs (`cell-N`), sources split after each `\n` as Jupyter stores them, UTF-8, written with `newline="\n"` so the file is byte-identical on Windows and Linux. It creates missing parent directories. `write_json(data, directory, name)` writes any notebook JSON the same way. The self-test validates with `nbformat` with warnings as errors, since `nbformat` repairs missing IDs with only a warning.
3. Deferred: options for CRLF line endings and non-UTF-8 residue, which belong on `write` since they are properties of the file.
4. The same `Notebook` value feeds the kernel runner (F7), so one scenario runs in file mode and in live mode. That gives file-versus-live parity checks for free (G15, K12).
5. Manifest-carrying notebooks come from running a real `snapshot` on a builder notebook. `manifests.snapshotted(directory, *cells, extra=())` snapshots `cells` once per session (cached by the cells) and returns a copy with `extra` cells appended, as a creator's later edits would be; the caller registers on the fake PyPI what the snapshot looks up, and a failed snapshot raises `RuntimeError`. `manifests.altered(path, edit)` applies `edit` to a written notebook's manifest literal in place and recomputes the hash through `models` (`SteadyPyManifest.from_literal(...).verified_hash`, the same hash check verifies): the one place tests change manifest content. It serves scenarios a real snapshot can't produce, such as an old notebook's baseline (LV1). `altered(path, edit, rehash=False)` keeps the stored hash, as a hand edit would (the tamper tests). The snapshot cache is keyed by the cells alone, not by the fake PyPI's state: a test that needs different PyPI state at snapshot time gives its cells a distinguishing comment (`code("import packaging  # stale at snapshot")`), or it gets another test's snapshot.
6. Saved notebooks: real-world patterns used by several tests or worth a human reading (a Colab-guarded install, a cookbook-style multi-install cell, a conda cell). `kitchen_sink.ipynb` and `magic_sink.ipynb` stay. Single-test scenarios are built inline, so test and data sit together.

## 5. F2 Runner and outcomes

1. One `Outcome` type for every way of running the tool: `exit_code`, `stdout`, `log` (text), `report` (parsed JSON or None), and `written` (paths, derived from the report's `artifacts_written`).
2. `run(*argv)` runs in-process through `cli.main(argv)` and catches `SystemExit`. It adds `--format json` unless a format is given. It captures stdout with `redirect_stdout`, and log output with a handler attached to the `steady_py` logger for the call, not stderr: `configure_console` binds `sys.stderr` on its first call and keeps it, so a later test's stderr capture would miss messages. The handler is a subclass of `StreamHandler`, so the CLI still installs its own stderr handler exactly as in real use. The logger's level, handlers and propagation are restored afterward, because `--quiet` and `--verbose` set the level, and `configure_console` turns propagation off, for the life of the process.
3. `run_in(venv, *argv, env=None)` runs `python -m steady_py` with the same arguments on the venv's interpreter and returns the same `Outcome`, with stderr as the log, so a test can move between layers without changing its assertions. It removes the inherited `PYTHONPATH`, `PYTHONHOME` and `VIRTUAL_ENV`, then applies `env` (such as `sites.pythonpath(...)`), so nothing from the test process leaks in.
4. Accessors over the report, the only code that knows its shape: `dependencies()`, `pins()` (canonical name to version, for entries Cell 2 installs; a commented entry is excluded, since the comment is the only reliable discriminator while `status` is wrong, P6), `dependency(name)` (the entry, or None), `warnings(type=None, about=None)`, `notices(type=None, about=None)`, `delta()`, `findings(signal=None, package=None)` for a check report (every severity list; each finding carries `severity` and `baseline_status`), and `summary()` for a directory report. Diagnostics match on `DiagnosticEvent.type`, never on wording. `about` is the one exception: `DiagnosticEvent` has no package field, and a warning a fix has yet to add has no type, so `about` matches a package name in the text. There is no cell filter: today's cell numbering is itself a finding (P7), so cell position is asserted only in P7's test, by notebook position. Tests assert `exit_code` before using accessors.
5. Directory reports have `notebooks()` (the paths covered) and `summary()`. `unreadable()` (paths that couldn't be read) and `validation()` (each distinct finding once, with the notebooks it's in) serve scan, snapshot and check alike, since scan and snapshot list unreadable notebooks in `summary.parse_errors` with absolute paths while check marks per-notebook entries with relative paths. Deferred: `raw_installs()`, and a fuller per-notebook view for directory reports (the single-notebook accessors raise `ValueError` on a directory report).
6. `manifest(path)` reads a written notebook with its own search for the `STEADY_PY_MANIFEST` assignment and `ast.literal_eval`: each code cell parsed alone (cells that don't parse are skipped; Cell 2 is plain Python), top-level assignments only, exactly one required. It deliberately doesn't use steady-py's extractor, which has its own bug (K1); an oracle must not share the code under test. `find_manifest(notebook_json)` is that search, shared with `manifests.altered`. `managed_cells(path)` maps each managed cell's `steady_py.role` tag to the cell; `setup_markdown(path)` returns Cell 1's text.
7. Rendered text is asserted only where the text is the behavior (E8, D6, runtime summaries), through `cell2_text(path)` (the setup code cell's text) and an installer Outcome's `stdout`, with a short comment in each test saying why text is the contract.

## 6. F3 Fake PyPI

1. A threaded `http.server` on `127.0.0.1`, one per session on an ephemeral port. The `pypi` fixture clears its registry per test and sets `STEADY_PY_PYPI_URL` and `no_proxy=127.0.0.1` through the environment, so it works in-process and in subprocesses.
2. Registry API: `pypi.add(name, releases={version: {...}})`, where each release takes `requires_dist`, `requires_python`, `yanked`, `yanked_reason`, `upload_time`, all optional. The fake builds PyPI-shaped JSON for `/pypi/<n>/json` and `/pypi/<n>/<version>/json` with only the fields steady-py reads. Names are normalized per PEP 503, and versions match by PEP 440 equality, as live PyPI does (`/pypi/packaging/21.0.0/json` returns 21.0); a local tag such as `+cu126` matches only itself. `info.version` is the highest final release. `upload_time` defaults to 30 days before the request, so heuristic staleness findings never appear as fixtures age.
3. Failure injection per project or per version with `pypi.fail(name, mode, version=None)`: `404`, `500`, `drop` (the connection closes with no status line; the client raises `RemoteDisconnected`) and `truncate` (a Content-Length longer than the body; the client raises `IncompleteRead`). Both of the last two escape `urllib` as raw `http.client` errors, which is K3's premise. A true timeout is left out: the client timeout is 10 seconds and not configurable, and the closed-connection case exercises the same handler.
4. Strict by default: a lookup of an unregistered project returns 404 and is recorded, and the fixture fails the test at teardown unless the test declared `pypi.allow_unknown()`. A silent 404 would otherwise masquerade as "not on PyPI". A missing version of a registered project is a plain 404, since removed versions are a scenario.
5. Deferred with K5, which awaits a decision (matrix decision 6): realistic graphs (torch's platform-marked `requires_dist`) load from trimmed real responses in `tests/fixtures/pypi/`, captured once by a small script and reduced to the fields used.
6. Later: the PEP 691 simple API, when index lookups for custom-index pins are built (CI1).
7. This replaces the three `offline` fixtures and the `urlopen` patch in `test_drift_check.py` when those files are migrated.

## 7. F4 Site directories and dist-infos

1. `SiteDir(path).add(name, version, *, requires, extras, modules, top_level, installer, direct_url, metadata_only)` writes `<n>-<version>.dist-info/` with `METADATA` (name, version, `Requires-Dist`, `Provides-Extra`), `INSTALLER`, `RECORD`, optional `top_level.txt` and `direct_url.json`, plus the module files it lists. `modules` are paths relative to the site dir (default: one package named after the project); `top_level=False` models backends that don't write `top_level.txt`. `RECORD` entries need no hashes; neither `importlib.metadata` nor `pip freeze` checks them.
2. Recurring shapes are keyword bundles for `add`, not subclasses: `CONDA_STUB` (INSTALLER `conda`, METADATA only, as conda-forge's OpenCV recipe writes), `vcs_ref(url, commit, subdirectory=None)`, `file_url(path)`. There is no editable shape: a `.pth` file is processed only in a real site directory, never in a `PYTHONPATH` entry, so the module wouldn't import. Editables come from real `pip install -e` in F5's per-test venvs.
3. Versions are written verbatim, so legacy versions (E6) and local tags (`2.6.0+cu124`) need no build tool.
4. Consumption is subprocess-only. The tool reads its own interpreter, and it runs `pip freeze` in a subprocess that sees `PYTHONPATH` but not in-process `sys.path` edits, so in-process use would give inconsistent environments. `pythonpath(*sites)` builds the environment entry (joined with `os.pathsep`) for a `run_in` call, in order, which also produces shadowing (E3, K6) without a second venv.
5. The writer's self-test uses the current interpreter in a subprocess as its independent oracle: `pip freeze` and `importlib.metadata` must both report each written shape as intended (plain with an extra, shadowed copy, legacy `===`, local tag, conda stub, VCS with `#subdirectory=`, `file://` archive, a namespace package shared by two distributions). Invented names are used, since real ones (`google`) are often provided by packages already installed.

## 8. F5 Venvs and wheelhouse

1. Wheelhouse: `tests/.wheelhouse/` (gitignored) holds the build backends (setuptools, wheel, hatchling, pdm-backend, editables), steady-py's dependencies (`packaging`, `resolvelib`, `ipython`), and ipykernel for the kernel tier. `ensure_wheelhouse()` first resolves them offline from the wheelhouse and downloads them only if that fails, so the tier needs the network once. If the wheelhouse can't be completed, the session fixture `steady_dist` skips the tier with pip's reason.
2. `steady_dist` builds steady-py's wheel once per session, offline, from a temporary copy of the packaging inputs (`pyproject.toml`, `src/`, and any `README*`, `LICENSE*`, `setup.*`). setuptools builds in the source tree, so building from the repository would leave `build/` there and collide between parallel workers.
3. `steady_venv(path, dist)` creates a venv and installs steady-py from `dist` with its dependencies from the wheelhouse: non-editable, so steady-py itself doesn't appear as an editable in the environment it inspects, and deterministic. Fixtures: `base_venv` (session, shared, never installed into) and `fresh_venv` (per test, about 3 seconds, the test may install into it). `Venv.python` abstracts `bin/python` versus `Scripts\python.exe`.
4. `install_project(venv, name, workdir, editable=False)` copies `tests/fixtures/projects/<name>` into the test's directory (builds write into the source tree) and installs it offline, with build isolation resolving backends from the wheelhouse. `uninstall(venv, *names)` removes distributions; uninstalling pip gives a venv like uv's (E1).
5. Stub projects in `tests/fixtures/projects/` are added as rows need a real build (editables: ED1, ED4, a pdm project). `demo-editable` (setuptools) exists. The existing `notebook_env_test_fixture` package folds in here under a new name. Packages that only need to be installed are written directly as wheels by `wheels.write_wheel(directory, name, version, requires=(), extras=(), source="")`: a py3-none-any wheel with one package, no build backend, milliseconds to write. The runtime rows use it (`demo-extras`, `demo-a`/`demo-b`, `demo-loc 1.0+cu126`, and an old `steady-py` for D2). `envs.pip_install(venv, wheels, *requirements)` preinstalls from such a directory and `envs.installed_version(venv, name)` reads a version back.
6. Cell 2 runtime tests install with `PIP_NO_INDEX=1` and `PIP_FIND_LINKS=<wheels>`, the mechanism development.md documents for Cell 2. `runner.install_in(venv, pins, wheels, raw_installs=(), python=<current>)` writes a minimal manifest and a script that calls `steady_py.install()` (public API) and prints the `InstallResult` on a marked line; the Outcome's `stdout` is what the runner sees and `install_result()` the result. `python` is the manifest's target, for the mismatch warning. The same script runs in Docker. `runner.run_cell2(venv, notebook, wheels)` runs a written notebook's Cell 2 text unmodified as a script, so a venv without steady-py installs its own helper from the wheel directory (which then needs steady-py's wheel and the wheelhouse); it returns an Outcome with no report. Both use one environment builder for the PIP_NO_INDEX/PIP_FIND_LINKS pair.
7. Parallel runs (`pytest -n auto`, pytest-xdist) work for both tiers; not adopted as a dev dependency yet. A first run with an empty wheelhouse should not be parallel, since workers would download into it at once.

## 9. F6 Local git repositories

1. A fixture copies a saved project into tmp, commits it, and returns a `git+file://...@<commit>` reference, with `#subdirectory=` for the monorepo.
2. Skips when git is missing.
3. Waiting on this: a real `git+file://` raw install at run time. The characterization test of raw-install ordering uses a local wheel path instead, since the installer passes every raw spec to pip as one argument, whatever its source; the VCS-specific part, capturing the line verbatim, is tested at snapshot.

## 10. F7 Kernel runner

1. `tests/support/kernel.py`: `kernel(venv, cwd, env=None)` starts a real kernel on `venv`'s interpreter through `jupyter_client`, with a kernelspec written beside `cwd` (so the kernel is the venv's, not the test process's) and `PIP_NO_INDEX=1`, so install lines in cells resolve against what's installed. The `live_venv` session fixture is the base venv plus ipykernel; it skips when the test process lacks `jupyter_client`.
2. `run_live(venv, cwd, cells, verb="scan")` runs the cells as typed, magics included, so IPython's own session history is what steady-py reads. It then makes the live call and prints the CLI's own JSON between markers, parsed into the same `Outcome`. The call runs with `store_history=False`, so the harness's import of steady-py is not part of the session it scans (a selftest proves it). Kernel stderr and cell tracebacks are the log.
3. Parity: each live test is parametrized over `mode` = `file` (`run_in` on the same cells saved as a notebook, the passing control) and `live` (the known bug), with one assertion for both. No separate parity helper was needed.
4. Deferred to the runner conversion (§11.3): `tests/runners/e2e_harness.py` keeps its own kernel manager, which runs the host's kernel with `src` on `PYTHONPATH`.

## 11. F8 Docker scenarios

1. `tests/support/docker.py` runs one scenario per test with `docker run --rm` on `python:3.11-slim` (not externally managed, unlike Ubuntu's system Python): `run_as_user(work, command, setup="")` runs `setup` as root, then `command` as a non-root user through `runuser`, with `work` bind-mounted at `/work` (`--mount`, so Windows drive letters parse) and pip offline from `/work/wheels`. The `docker_work` fixture fills `wheels/` with steady-py's wheel and the wheelhouse, and skips the test when Docker can't run. `require_scenario` raises `RuntimeError` when the container didn't set up the condition under test, so a broken scenario can't pass as an expected failure.
2. Scenario built: a non-root user on a root-owned system Python, where pip falls back to the user site that the running interpreter can't see (R4, R5, D2). The premise was checked on a Linux host Python as a non-root user; the containers themselves are run on the Windows host.
3. Later: the existing runners become pytest-collected `docker` tests, so results are per test and xfail works, with `run_suite.py` a thin driver; and a Miniconda image for a one-time confirmation of the conda rows (E2b, ED4's `conda develop`, C1).

## 12. Production changes made

1. `pypi.py`: the PyPI base URL comes from `STEADY_PY_PYPI_URL` (default `https://pypi.org`), read on every call. It's also mirror support; README and HELP don't mention it yet.
2. `cli.py`: `main(argv=None)` passes `argv` to `parse_args`, so the in-process runner doesn't patch `sys.argv`. Behavior is otherwise unchanged; the console script and `__main__` call it with no arguments.
3. Characterization pass: no production changes.

## 13. To verify while coding

1. Build isolation resolves hatchling and pdm-backend from find-links offline, on Windows and Linux (setuptools confirmed on Linux).
2. `git+file://` URL form for Windows drive paths.
3. `DiagnosticEvent.to_dict` omits `level`; warnings and notices are separate lists, so matching by `type` within the list should suffice. Confirm no two diagnostics share a `type` with different meanings.
4. Whether per-session base-venv creation (about 6 seconds on Linux once the wheelhouse exists) should be cached across sessions, keyed on a hash of the packaging inputs.
5. The ANSI code-page case in E1 runs only on the Windows host.
6. Windows: with no proxy environment variables, `urllib` reads the registry proxy settings; a system proxy without a `<local>` bypass would route requests to the fake PyPI through the proxy.
7. Windows: when the dev environment is itself a venv, `python -m venv` bases the new venv on the underlying installation, as intended.
8. How PyPI chooses `info.version` when the newest release is yanked; the fake counts yanked releases. It matters only for the stale and newer-major heuristics.
9. Suite time: each in-process run spends about 1 second in steady-py's own `pip freeze` subprocess, which dominates the default run (about 95 seconds on Linux). Sharing snapshots (§4.5) saved about 12 seconds. The remaining lever is pytest-xdist on a multi-core host.
10. Docker: `runuser` and `useradd` are present in `python:3.11-slim`, and the bind mount is readable by the non-root user on Docker Desktop for Windows and macOS (Apple Silicon pulls the arm64 image; every wheel used is py3-none-any).
11. Verified: the kernel tier on Windows (Python 3.13), 7 passed and 7 xfailed, matching Linux.

## 14. Existing tests at the rearchitecture

Each older test file, by what it couples to and what replaces it. "Breaks" means it imports module internals or patches them, so it fails as soon as those move; it is deleted once its replacement covers it, and not repaired. "Gap" names behavior that no boundary test (`tests/behavior/`, `tests/characterization/`) covers yet; those tests stay until a replacement is written or the behavior is cut.

Gaps referred to below:

1. Accelerator detection and hardware tags (torch, TensorFlow, JAX, fastai's transitive torch; GPU recorded in the manifest and markdown).
2. Notebook language detection: non-Python kernels skipped, conflicting or missing metadata, majority and tie-break rules.
3. Directory walks skipping hidden and `.ipynb_checkpoints` directories.
4. Guarded and dynamic imports: guarded packages written as optional, an unconditional import overriding a guarded one, literal and variable `importlib.import_module`.
5. Index-URL harvesting rules: per-package scoping, last-wins conflicts with a warning, directory-level aggregation.
6. The JSON report contract: schema and tool-version fields, field shapes, console and JSON agreeing on counts. Boundary tests read the JSON but don't pin its shape.
7. Delta for Python version, GPU, and findings that appeared or resolved (package changes are covered).
8. Cell 2 runtime edges: install timeout, pip that can't start, scoped flags shown on failure, the custom-sourced failure pointing to the author, the timeout baked into generated cells.
9. `--universal` and `--full-freeze`: pending the scope cut (matrix decision 1), so not gaps to fill yet.

Top-level `tests/test_*.py`:

| File | At the rearchitecture | Replaced by | Gaps |
| --- | --- | --- | --- |
| `test_batch_failures.py` | Breaks: calls analyze/generate directly, patches `get_installed_environment` | characterization/test_directory_runs.py | 2, 3, 5 |
| `test_batch_mode.py` | Breaks: same coupling | characterization/test_directory_runs.py, test_local_modules.py; behavior/test_environment.py (directory scan), test_writing.py | 1, 2 |
| `test_cli.py` | Breaks: 60 patches of endpoints and drift | characterization/test_check_signals.py (exit code per signal), test_directory_runs.py, test_manifest_lifecycle.py; behavior/test_writing.py (text report) | none; its heuristic exit-code cases are the subject of matrix decision 8 |
| `test_constants.py` | Survives | | The module list in `test_scan_covers_every_module_in_the_package` tracks the new layout; the manifest-literal case builds a baseline through `drift` |
| `test_delta.py` | Breaks if `compute_delta` moves (pure, no patches) | characterization/test_scan_delta.py; behavior/test_writing.py (P3) | 7 |
| `test_direct_references.py` | Breaks | behavior/test_environment.py (E5, E8), test_install_lines.py (G12); characterization/test_setup_runtime.py (VCS line verbatim) | none known |
| `test_disk_output.py` | Breaks | characterization/test_manifest_lifecycle.py; behavior/test_writing.py (K7) | 1 |
| `test_drift_check.py` | Breaks: patches `_fetch_pypi_json` and drift internals | characterization/test_check_signals.py; behavior/test_validation.py | Extras in the resolution graph walk (which extras are walked, conflicts an extra creates) |
| `test_drift_check_live.py` | Breaks: calls `drift` and `pypi` against real PyPI | behavior/test_validation.py against the fake | The fake's response shapes are trimmed real responses; a slim real-PyPI contract check is still worth keeping |
| `test_endpoints.py` | Breaks: 40 patches | characterization (all files); behavior/test_live.py for the live-session target | 1, 8 |
| `test_installed.py` | Breaks: pin-string parsing internals | behavior/test_runtime.py (R1, R3) | none |
| `test_json_format.py` | Breaks: patches `sys.argv` and the environment | none | 6 |
| `test_magic_harvesting.py` | Breaks: calls `magics` and `scanning` directly, no patches | behavior/test_install_lines.py, test_reading.py (G13) | 5 |
| `test_manifest_roundtrip.py` | Breaks | characterization/test_manifest_lifecycle.py, test_local_modules.py; behavior/test_reading.py (K1) | none known; the malformed-entry cases border CH1 |
| `test_module_hygiene.py` | Survives, except `test_failed_opencv_probe_falls_back_and_is_logged_at_debug`, which patches `resolution.subprocess` | | |
| `test_results.py` | Mostly survives: the result types are public API, but its helpers build them from `models` and `drift` | | |
| `test_runtime.py` | Breaks: patches `subprocess.run` | characterization/test_setup_runtime.py; behavior/test_runtime.py | 8 |
| `test_steady_py.py` | Breaks: 57 patches across most modules | behavior/test_reading.py, test_install_lines.py, test_pins.py; characterization/test_distribution_names.py | 1, 4 |
| `test_steady_py_fixtures.py` | Breaks: patches the environment and OpenCV probe | characterization/test_distribution_names.py; behavior/test_pins.py (K9) | none known |
| `test_structural_fixtures.py` | Breaks: same coupling | characterization (all files) | none known |

`tests/runners/` (docker tier, driven by `run_suite.py`): all survive the rearchitecture unchanged in form, since they run the CLI and kernels as subprocesses, but their assertions on Cell 2 and report text will need updating as that output changes.

| File | Replaced by | Notes |
| --- | --- | --- |
| `run_suite.py` | | Kept as the thin driver after the conversion (§11.3). The Kaggle, Colab and python3.11 image tiers have no other coverage |
| `e2e_harness.py` | `tests/support/kernel.py`, `docker.py` | Retired by the conversion |
| `test_check_drift.py` | characterization/test_check_signals.py, test_manifest_lifecycle.py | Candidate for deletion at the conversion |
| `test_hardware_mock.py` | none | Only end-to-end coverage of gap 1; convert, don't drop |
| `test_live_kernel_phase0_regressions.py` | behavior/test_live.py | Candidate for deletion at the conversion |
| `test_live_kernel_stale_repin.py` | The restart note is covered by characterization/test_setup_runtime.py | The in-kernel staleness it demonstrates is Python's behavior; keep as a demonstration or drop |
| `test_raw_installs.py` | characterization/test_setup_runtime.py (raw installs after pins, from local wheels) | Its URL-served and unreachable-source cases have no other coverage; its wheel comes from `wheels.write_wheel` |
