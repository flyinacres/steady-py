# Development notes

For whoever develops steady-py: design decisions and their constraints, known bugs, open questions, and the task list. To use the tool, see `README.md`.

**Guiding principle**: what is best for the users, most of whom are not software engineers. "False success is worse than nothing" means the tool never hides a gap; it does not mean it refuses to produce output. It stops only when it cannot proceed or when continuing would do harm; otherwise it continues and reports.

## Known bugs

- **The live-session scan counts steady-py itself as a dependency.** `scanning.extract_from_active_session` filters out cells that invoke the tool, but it recognizes only the paste-era forms (`import steady_py` alone in a cell, `spy.main()`). A cell like `import steady_py` followed by `steady_py.snapshot()` passes through, so `steady_py` is reported as an import of the notebook.
- **Live-kernel local modules outside `notebook_dir` are all tagged `root_dir`.** In a live kernel, `resolve_local_module` resolves with an unrestricted `find_spec` and distinguishes only `notebook_dir` from everything else, so a module found through PYTHONPATH, an editable install, a Databricks Repos root or an earlier `sys.path.append` is recorded as `root_dir`-anchored even when no `root_dir` was given. `check` then re-verifies it under the supplied `root_dir` and reports it missing or unverifiable, neither of which reflects reality. The fix belongs in what snapshot records, not in check.
- **Cell 2 can silently use a different steady-py version.** If `pip install steady-py==<version>` fails, Cell 2 warns and still runs `import steady_py`. An already-installed, different version (a leftover, another notebook's setup in the same session) then runs `install()` with only the earlier warning as a trace. Fix: compare the imported version with the pin.
- **Every install failure gets the same advice.** `install()` reports each failure's pip output, but the closing troubleshooting block always suggests checking internet access first, whatever the cause (a bad wheel, a timeout, a wrong Python). Cause-specific advice needs its own design pass.

## Limitations by design

- `--output-dir` mirrors notebooks only, not their sibling files (`data/data.csv`), so a notebook that reads relative files won't run from the output location. Copying arbitrary files would make this a sync tool.
- Directory discovery skips files whose names end in the companion suffix (default `_merged`), so a real notebook named that way is never processed. This is separate from cell-level replacement (`is_prior_setup_cell`, `metadata.steady_py.managed`).
- Outside a live kernel, local-module detection searches only `notebook_dir` and `root_dir`; a `sys.path.append(...)` in the notebook is invisible. Extracting literal `sys.path.append`/`os.chdir` arguments was designed, not built.
- Dependencies selected by a string argument (`holoviews.extension("bokeh")`, matplotlib backends, pandas `engine=`) never appear as imports, so no static scan sees them.
- Pins are checked against metadata only; code that installs cleanly but breaks at call time is not caught.
- One manifest holds one pin per top-level import, from the environment at snapshot time. A notebook meant to run on CUDA and on MPS can't carry both; regenerating on the second machine replaces the first.

## Architecture

**Names**: PyPI and console-script name `steady-py`, import name `steady_py`, source under `src/steady_py/`, also runnable as `python -m steady_py`. Repository: `github.com/flyinacres/steady-py`. `packaging` and `resolvelib` are base dependencies, with no extras, so `pip install steady-py` always yields a working tool.

**Layering** (a module imports only from modules above it in this list):

1. `constants` (version numbers, label constants, static lookup tables), `models` (shared dataclasses, finding keys), `util` (package-name normalization, IPython detection, stderr silencing, per-run memoization, relative notebook paths).
2. `installed` (the running interpreter's pins and direct references, pin-string parsing), `pypi` (the PyPI JSON client).
3. `scanning` (notebook and live-session reading, cell classification, the AST scan), `magics` (pip, conda and system installs, index URLs, scoped flags), `localmodules` (sibling-module resolution and its check), `accelerator` (GPU detection).
4. `resolution` (the install/import timeline, imports to dependency entries).
5. `drift` (pin checks, transitive graph, baseline, drift and batch-validation reports).
6. `analyze` (single-notebook and directory analysis).
7. `generate` (manifest, setup cells, universal manifest, writing locked notebooks and reading their manifest back).
8. `reporting` (console and JSON formatting); `runtime` (`install`, which needs only `constants`).
9. `results`, `delta`, `endpoints`, `cli`, `__main__`.

Module names avoid names the code uses for locals and parameters (`environment`, `hardware`, `analysis`, `blueprint`), hence `installed`, `accelerator`, `analyze` and `generate`. The package is flat; subpackages aren't worth the longer import paths at this size.

**Calling convention**: package modules call functions through their module (`drift.run_pin_checks(...)`) and from-import only classes and constants. Tests patch a function on its defining module, which only takes effect when callers look it up there at call time; `test_module_hygiene.py` fails on any function from-import outside `__init__.py`. monkeypatch raises when the patched name doesn't exist, so a patch aimed at the wrong module fails loudly. Some patches only isolate tests from the machine (`inspect_gpu_environment`, `resolve_opencv_variant`) and are not proven to take effect, because on a machine without GPU frameworks or OpenCV the real function returns the same value.

**Public API**: `__all__` in `__init__.py`. A leading underscore means package-private: other steady_py modules may call it, users should not. `TOOL_VERSION` (from the installed package's version), `SCHEMA_VERSION` (the `--format json` report) and `MANIFEST_SCHEMA_VERSION` (the manifest's own structure) live in `constants`.

**Logging**: the package logger `steady_py` is set up in `__init__.py`, which runs before any submodule: a NullHandler, level INFO, no propagation to the root logger, so a host that configures logging (IPython, pytest) doesn't print everything twice. Modules log through `steady_py.<module>`. `cli.configure_console` attaches the stderr handler and forces UTF-8; importing the package never touches the standard streams. The NullHandler is added only when the logger has none, so reloading the package never stacks handlers.

**Per-run caches**: `util.memoize_for_run` caches by argument value (equal dicts, sets and lists share an entry) and registers every cache; `util.reset_run_caches` clears them all. Each endpoint call is one run and starts with a reset, so a batch shares lookups across its notebooks while a second call in the same process (a live kernel, a program using the API) sees files, installs and PyPI releases that changed in between. Nothing is cached across runs: yanked status and latest versions are what the checks exist to catch.

**Best-effort probes** (GPU, OpenCV variant, extras tagging) stay quiet in the expected case and log at debug otherwise. The TensorFlow device-name lookup is deliberately silent: it runs inside `silence_fd2_stderr()`, where a log line would be lost.

## Verbs, results and exit codes

- `scan`: read-only. Reports what a notebook needs, with warnings and notices; never contacts PyPI. With an existing manifest, also reports the delta against what a snapshot would produce now.
- `snapshot`: the same analysis, producing the manifest and the two setup cells, validated against PyPI. It returns the cells, or writes them: `--output` (companion file), `--output-dir`, `--in-place`. An existing manifest is replaced only with `--in-place`. Shows the same delta as scan.
- `check`: read-only. Compares a manifest's pins with live PyPI. No manifest means nothing to check (exit 0).
- `install` (runtime): what generated Cell 2 calls. Needs internet; internet-off runs are unsupported.

Each verb takes a file or a directory; the live IPython session is the target when the API is called with none. A directory is a larger target, not a separate mode: results hold a list of per-notebook results plus an aggregate. `--universal` (the combined requirements file) is the one directory-only option. The endpoints return typed results (`results.py`) and never print or exit; options are small dataclasses; the environment is injectable for tests; the CLI formats a result and maps it to an exit code.

**Delta**: packages added and removed, pin versions changed (reported separately, since pins come from the machine the tool runs on), Python version and GPU changes, and baseline findings that appeared or were resolved.

**Write rule**: partial writes, loud failure. A directory run writes every notebook that parsed, lists the others with the reason, and exits 1. The universal file begins with a comment naming skipped notebooks.

**Exit codes**, one rule for every verb: 0, everything processed cleanly; 1, the job was done but something needs attention (drift found, notebooks skipped); 2, the job could not be done (bad arguments, a missing path, an unparseable single file, a directory where nothing could be processed, a pin or manifest check couldn't verify). For check on one notebook, 1 is any confirmed finding, new or known at generation, or a heuristic finding not already known; a known heuristic finding or a known custom source does not fail it. Across a directory, check exits 2 only when no notebook could be checked; a notebook without a manifest counts as clean.

**Cell 2** is a few lines: `pip install steady-py==<generating version>`, then `steady_py.install(STEADY_PY_MANIFEST, timeout=...)`. The manifest literal stays in the cell so check can read it. For local testing, pip's own `PIP_NO_INDEX`/`PIP_FIND_LINKS` make the unmodified cell install from a local wheel.

**install()** installs pins one at a time, so one bad pin never blocks the rest, then raw installs. It skips pins already satisfied, warns (and carries on) on a Python mismatch, reports a pin whose install moved an earlier package's version, and never raises: it returns an `InstallResult` (total, installed, failed specifiers, `ok`). The restart-the-kernel note appears only when something was actually installed.

## Manifest and drift checking

**Manifest** (`SteadyPyManifest`, the `STEADY_PY_MANIFEST` literal in Cell 2): `schema_version`, `python_version`, `dependencies` (direct pins), `gpu`, `generated_at`, `tool_version`, `raw_installs`, `custom_sourced` (pins not found on PyPI), `local_modules`, `baseline`, `dependency_hash`. No external file, which avoids path ambiguity on Kaggle, Colab and multi-notebook directories.

**`dependency_hash`** covers the whole manifest, content and provenance, as sha256 over sorted-key JSON. It detects hand-editing, including an edited timestamp or a deleted finding. Verification hashes the fields as persisted, so adding a field never makes an older manifest look edited.

**Replacement is unconditional**: snapshot always produces a new manifest (new timestamp, version, hash and baseline). Writing replaces every prior setup block: cells tagged `metadata.steady_py.managed`, plus untagged cells that assign `STEADY_PY_MANIFEST` or begin with the setup heading, since pasted cells lose the tag. Pins come from the environment the tool runs in, so fixing a flagged pin means changing that environment and regenerating; hand edits are what the hash catches.

**Check's signals**, per pin and through the transitive graph (resolvelib, with environment markers evaluated and extras modeled as their own nodes, as pip does): yanked release, project removed from PyPI, pin-to-pin conflict, no support for the notebook's Python (all confirmed); no release in about two years, a newer major version (heuristic); plus local modules missing (see below). Confirmed and heuristic findings are reported separately so heuristics don't erode trust in the confirmed ones. `run_pin_checks` is the single implementation behind generation-time validation and check.

**Deferred**: vulnerability data (PyPI's JSON already carries OSV entries, but a matched CVE in unreachable code is noise for a notebook; if added, keep it a separate signal) and license checking.

**Baseline**: generation records what its validation found, as keys (never messages), in the manifest's `baseline`: `None`, or `{"version": 1, "findings": [keys], "errors": [packages]}`. A key holds exactly the facts that define the problem, so a changed fact is a new finding: `yanked`, `removed`, `not_found_on_pypi`, `unsupported_python`, `unverifiable_custom_index` key on signal, package and version; `stale` on signal and package; `major_bump` adds the latest major; `conflict` adds specifier and parent. `tampered`, `local_module_*` and `check_error` have no key. At check time each finding is `known`, `new`, or `not_checked_at_generation` (its package errored then). An unrecognized or missing baseline is reported flat. A known `not_found_on_pypi` is an expected private package: a notice, never affecting the exit code. Every JSON finding carries `key` (`finding_identity_key`), so two reports compare by key sets. Regenerating resets the baseline.

**Batch validation**: in write modes, each notebook's generation-time findings fold into one view (`build_batch_validation`): each distinct finding once with the notebooks it affects, deterministic (sorted, relative paths, no timestamps). It never changes the exit code; generation is not a check.

**Non-PyPI sources**: `%pip install` tokens that are paths, VCS URLs or `http(s)://` are carried verbatim in `raw_installs` and installed after the pins. Packages installed by hand from a URL, path or editable install are found through `direct_url.json` (PEP 610), not `pip freeze` text. A remote one goes into `raw_installs` unless the notebook's own install line names the same source. A local path or editable install is reported as system-dependent and never stored, so no private path leaks into a shared notebook. Check verifies nothing about raw installs, and their dependencies aren't walked.

**Local modules**: in a live kernel, `resolve_local_module` uses an unrestricted `find_spec`, which is ground truth for whatever the platform does. Outside one, it searches only `notebook_dir` and `root_dir` with `PathFinder.find_spec(name, path=[dir])`, which neither mutates `sys.path` nor executes `__init__.py`. The manifest records each as `{"name", "anchor": "notebook_dir" | "root_dir"}`, never a path, because a path under `root_dir` could reveal directories outside the shared project. Check re-verifies by plain file existence, so the answer doesn't depend on where it runs: still present, nothing; anchor directory gone or no `root_dir` supplied, could not be checked (2); directory present but the module gone, confirmed (1).

## Testing

**Tiers**: unit tests (`pytest tests --ignore=tests/runners`); live-PyPI tests in `test_drift_check_live.py`, skipped unless `RUN_LIVE_PYPI_TESTS=1`; Docker and live-kernel runners in `tests/runners`, driven by `run_suite.py`. Coverage: `python -m pytest tests --ignore=tests/runners --cov=steady_py --cov-branch --cov-report=term-missing`. Last run: 610 passed, 13 skipped; mypy (`--ignore-missing-imports`) clean. `mypy --strict` reports 14 errors, half of them the untyped resolvelib provider.

**Where tests live**: mostly one file per module (`test_runtime.py`, `test_installed.py`, `test_endpoints.py`, `test_cli.py`, `test_results.py`, `test_delta.py`, `test_magic_harvesting.py`, `test_direct_references.py`, `test_drift_check.py`); batch behavior in `test_batch_mode.py` and `test_batch_failures.py`; writes in `test_disk_output.py`; fixtures in `test_steady_py_fixtures.py` and `test_structural_fixtures.py`; guards in `test_constants.py` (named label constants, not bare strings) and `test_module_hygiene.py` (import side effects, the calling convention, reload). `test_steady_py.py` is the older catch-all and still holds tests that belong elsewhere.

**Fixtures**: `kitchen_sink.ipynb` is the main AST edge-case fixture; `magic_sink.ipynb` covers magics, and its `%%writefile` must be the cell's literal first line. Delete stale `_merged` companions from a fixture directory before a directory run, or they are scanned as notebooks.

**Container tiers**: one container per tier (Kaggle, Colab, plain slim), with a fresh venv per run; platform tiers need `--system-site-packages` or the venv hides the preloaded environment they exist to test. Containers install the package from the mounted repo (`pip install -e /workspace -q`). Pin `nbconvert==7.17.1` in every tier (Kaggle and Colab ship different versions). Slim images need `ipykernel` installed and registered. The Colab image needs `--entrypoint bash` (or `python3`) to skip its web server; its kernel config lives at the system IPython level, so the bypass keeps the real kernel setup. Pass `--platform linux/amd64` on Apple Silicon. A notebook's final cell should check both `importlib.metadata.version` and a real import. The images are large on disk (Kaggle about 25 GB, Colab about 49 GB).

**Lessons**: run the whole suite regularly, since stale tests only fail when something else changes. A test that assigns `subprocess.run` directly must register it with `monkeypatch.setattr` first, or the replacement outlives the test.

**Coverage gaps**: two GPU frameworks both active (a tensorflow-only notebook must pick tensorflow's device from `framework_devices`); `--output`/`--in-place` against a real corpus notebook; `--output-dir` idempotency; notebooks with no `kernelspec`/`language_info` being accepted as Python; the `--check-drift on this file` assertion in `test_drift_check.py`, which tests a string no longer in the source and so cannot fail.

**Open container items**: compare version markers from a live Colab session with the pulled image before claiming anything about current Colab; confirm the setuptools/wheel probe results for Kaggle and Colab weren't saved under the same filename; decide whether the plain tier moves to `python:3.12-slim` to match the platform images. A lighter extras-promotion fixture than `umap-learn` has not been found (the promotion needs a real dotted submodule import).

## Real-world validation

The corpus (`test_notebooks/`, gitignored) has 147 notebooks: Kaggle notebooks with reproducibility labels (MLEModernizer, `huggingface.co/datasets/BihuiJ/MLEModernizer`), `amazon-sagemaker-examples`, Databricks examples, papermill examples, course material, and personal notebooks. `diagnose_coverage.py` (not shipped) reports which code paths each notebook exercises. Not yet done: the loop this is for — snapshot a notebook, install only its manifest into a clean container, and confirm it still runs. GPU notebooks need manual testing on Kaggle.

## Open design questions

- `--full-freeze`: stays additive (appended after the manifest) or becomes a replacement? Embedding a freeze in notebook metadata is unreliable (frontend autosave), which blocks a single-file form.
- Strip hardware build tags (`+cu121`) before pinning? Unverified as safe; they are flagged, not stripped.
- Databricks source-format `.py` files: a second ingestion path, or stay `.ipynb`-only?
- A lookup table for string-selected backends (`holoviews.extension("bokeh")` → `bokeh`), or document it as a permanent limit?
- Should Cell 1 also tell users to restart the kernel if cells below already ran? `install()` prints a restart note after any install.
- Record a raw install's declared requirements at generation so check can walk them?
- Opt-in check inside the notebook's own environment for custom-sourced pins that PyPI can't verify?
- Is `--format json` independent of `--quiet` for stderr? (Leaning yes.) Should writes get a JSON form beyond `artifacts_written`?
- Does a change only in a pin's flags (a different index URL) count in the delta?
- Rename the fixture package `notebook_env_test_fixture` (its wheels need rebuilding) and delete the tracked temporary fixture notebook?

## Cleanup backlog

- The magic dispatch in `magics.harvest_cell_magics_and_commands` is an order-dependent if/elif chain (`SYSTEM_PKG_PATTERN`, `CONDA_INSTALL_PATTERN`, `PIP_INSTALL_PATTERN`); a dispatch table would suit a growing set of magics.
- `analyze.analyze_batch_repository` has four near-identical dedupe loops.
- The `--format json` payloads in `reporting` are built as literal dicts, not a typed structure.
- A bare `except Exception` around `packages_distributions()` in `resolution.resolve_pypi_package_and_extras` should log at debug.
- `generate.generate_production_blueprint`'s custom-sourced loop repeats a guard in `drift.run_pin_checks`.
- `generate_batch_analysis_report`, `process_package_requirements` and `harvest_scoped_cell_flags` are used only by tests; kept for their tests. `DependencyEntry.raw_token` is unused; `PypiVersionMetadata.project_urls` is reserved for the changelog link.

## Roadmap

- Cross-notebook caching for directory runs: a per-import cache keyed on `(import, frozenset(submodules), is_guarded, is_local_here)`, never on the import name alone, since guarding, submodules and local shadowing differ between notebooks. Built as an explicit dict, it also yields a reverse index for free: which notebooks use each package, framework usage counts, extras-promotion frequency. The point is summaries that show non-engineers what needs attention, grouped by notebook. Validate the overlap assumption against the corpus first.
- `why <package>` (which pin pulled it in), a changelog link on major-bump findings, and major/minor/patch tiers.
- A static-only scanner for notebooks you don't own.
- Handle `%run`; warn on `exec()`/`eval()`; flag bare relative imports (none found in the corpus so far).
- Grow `IMPORT_TO_PYPI_MAP` as mismatches are found (`dotenv`, `mpl_toolkits` so far).

## Task list

Tasks 1–18 are done: the rename and `src` layout, typed results, the `scan`/`snapshot`/`check` endpoints and subcommands, directory targets, the delta, partial writes and the exit-code rule, the runtime installer and slim Cell 2, real base dependencies, the manifest schema version, and the split into modules.

19. TODO. Handle guarded imports and installs better (gaps collected in an earlier chat thread).
20. TODO. Point `HELP_URL` and the README at `github.com/flyinacres/steady-py`.
21. TODO. Review the tests and coverage: what is missing, what is duplicated, what is misplaced.
22. TODO. Review all user messaging: is it helpful and appropriate?
23. TODO. Hand-test every mode for roadblocks.
24. TODO. Rewrite the README and HELP.
25. TODO. Add a license file.
26. TODO. First release.
