# Test plan — steady-py

Goal: show that steady-py works on real notebooks in real environments, not just that it runs without crashing. This page tracks what is covered and what is still open, in priority order. Where the tests live and how to run them is in `development.md` → Testing.

Log findings as you go:

- A bug → `development.md` → Known bugs (what's wrong, where, the notebook that showed it).
- An import flagged missing that shouldn't be → the import name and its PyPI name, for `IMPORT_TO_PYPI_MAP`.
- An environment or scenario you didn't get to → say so here, so it isn't assumed covered.

**Discipline**: write a new test so it fails first for the intended reason, then sabotage it (break the behavior on purpose) and confirm it fails. Several fixtures have passed vacuously until this was done. Check every claim from a reviewer, human or LLM, against the code or the corpus before acting on it.

---

## What is covered today

- **Unit tests**: 610 passing, 93% statement-and-branch coverage. Scanning, magics, resolution, drift checks and baseline, generation and writing, endpoints and CLI, the runtime installer, JSON output, directory runs and their failure modes.
- **Live PyPI**: a few tests in `test_drift_check_live.py` and a round trip in `test_manifest_roundtrip.py`, run with `RUN_LIVE_PYPI_TESTS=1`.
- **Docker tiers** (`tests/runners/run_suite.py`):
  - *common*: check end to end; raw installs (explicit path, inferred URL, unreachable source, inferred local path) against a hermetic local wheel server; live-kernel runs (a stale module after a re-pin, and `steady_py.snapshot()` inside a kernel); GPU detection with fake torch packages (CUDA and Apple MPS); the self-owned test package (pin and re-pin, install timeout, build failure).
  - *python3.11*, *kaggle*, *colab*, *local_pkg*: generate, execute Cell 2 and the notebook, and verify. The install engine under partial failure: one bad re-pin doesn't block a sibling, a failed re-pin surfaces downstream, and a correct pin preserves an old API (`numpy.bool`, removed in 1.24). Negative fixtures are verified by parsing the executed notebook for the expected exception.
- **Corpus**: 147 real notebooks, scanned in directory mode; `diagnose_coverage.py` reports which code paths each exercises.

---

## Open work, in priority order

### 1. Hand-test every mode in real environments (task 23)

Run both forms in each environment: the CLI on a saved file (`steady-py snapshot my_notebook.ipynb --output`), and the live session (`steady_py.snapshot()` in a cell, as in the README). The live form is where the session bugs have been found.

| Environment | What to check | Priority |
| --- | --- | --- |
| Colab | `google.colab` is reported as provided by the platform, not missing. Not yet confirmed on a real Colab notebook: the biggest gap. | High |
| Kaggle, GPU | Real CUDA detection; the hardware-build warning for a `+cu121`-style pin. | High |
| Kaggle, CPU | The GPU section is absent, not wrong; a second snapshot in the same session behaves the same. | High |
| Mac (Apple Silicon) | Real MPS detection, and the "torch imported, no accelerator" case. | High |
| Local CUDA machine | Real CUDA detection against real hardware. | Medium |
| Databricks | A `.py` source-format notebook fails clearly, not by silently mis-parsing. | Medium |

Also walk the README and HELP.md examples against real output; wording drifts.

- [ ] The live session reports `steady_py` itself as an import (see Known bugs). Add a regression test to the live-kernel runner once it's fixed.

Pass bar: for each cell, either the output matches what reading the notebook says it should, or a bug is logged. "Didn't look closely" is untested, not a pass.

### 2. End-to-end reproducibility

The test of the tool's actual claim: snapshot a notebook, install **only** its manifest into a clean, minimal container (not a platform image, whose preinstalled packages would hide a gap), run the notebook, and record the result.

- [ ] Sample of 5–10: a couple of plain notebooks, one heavy on guarded imports, one with a hardware-tagged package if available. GPU notebooks directly on Kaggle.
- [ ] Root-cause every failure: a generation bug, or a gap no manifest can close.

Pass bar: most of the sample runs from the manifest alone, and every failure is explained. Full reproducibility isn't a binary outcome: real notebooks also need data, credentials, GPU time, or backends selected by a string (`holoviews.extension("bokeh")`), none of which Cell 2 provides. Bucket failures by cause rather than counting pass/fail.

### 3. File and directory runs agree

- [ ] A helper that runs snapshot on a directory and on each file in it, compares the manifests (via `--format json`), and fails on unexplained differences. The bugs this catches (cross-notebook cache leaks, wrong attribution) have occurred before. Run it over a stratified sample: GPU notebooks, notebooks with local helper modules, a hardware-tagged package, and plain ones.

### 4. Remaining automated gaps

- [ ] Idempotency for `--output-dir` (running twice leaves one set of setup cells); `--output` and `--in-place` are covered.
- [ ] GPU detection for TensorFlow and JAX (fake packages, as for torch), and two frameworks active at once.
- [ ] Live-kernel scenarios beyond linear runs: Cell 2 run twice, cells out of order, restart then only Cell 2.
- [ ] Environment conditions in Docker: no network (`--network none`), a read-only source directory for the write modes, CRLF notebooks, non-UTF-8 residue in cells, non-ASCII paths.
- [ ] Negative fixtures for the kaggle and colab tiers (lower priority: the install engine is shared code).
- [ ] The fix-and-regenerate loop in Docker (a yanked pin replaced, `--in-place` rerun, baseline resets, check exits 0; so far checked by hand); raw installs from a git URL (needs a network or a local git server).

### 5. Goldfiles

A small, hand-verified set, kept small on purpose because each one costs a human decision:

- [ ] One minimal notebook per feature, with its exact Cell 1 and Cell 2 checked: guarded import, platform module, hardware-tagged package, conda line, each GPU framework, local helper, index-URL conflict.
- [ ] The `--format json` structure, as a stability contract for anyone scripting against it.
- [ ] One deliberately plain notebook that should essentially never change. Candidate from the corpus: a scikit-learn/`feature_engine` notebook with no unusual magics.

### 6. Corpus regression

- [ ] Save the JSON output for the corpus and diff future runs against it, reviewing every difference. This detects change, not correctness, so it complements goldfiles rather than replacing them.

### 7. Cloud smoke runs before a release

- [ ] `kaggle kernels push` on a fixed small set of notebooks. Too slow and quota-bound for everyday use.

---

## What the corpus has taught

- The corpus is nearly uniform: no hardware-tagged packages, no editable installs, one custom index URL, no sibling config files, no local helpers, no relative imports across 147 notebooks. It is good for regression on the common case (pandas/numpy/matplotlib, plain `%pip install`), and useless for structural edge cases. Those come from synthetic fixtures (`test_structural_fixtures.py`) or from cloning whole projects, not single notebooks.
- One notebook's saved output shows `!pip install pykeops` followed by `ModuleNotFoundError` in the same session: the stale-module-after-install case is real, which is why `install()` now prints a restart note.
- Backends selected by a string (`hv.extension("bokeh")`) are a real, statically invisible dependency.
- Enterprise projects often manage environments outside any notebook (a team `requirements.txt`, a base image, an index configured at the venv level). A tool that reads one notebook is blind to that by construction.
- `diagnose_coverage.py` misses `python -m pip install`, doesn't count `-r requirements.txt` separately, and hardcodes its platform-module list instead of reading `constants.PLATFORM_PSEUDO_MODULES`. None of this changes the conclusions above.
