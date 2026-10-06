# Test triage matrix for review findings

Purpose: decide, per finding in review_findings.md (plus the open known bugs in development.md, prefixed D), whether a test can be written before the rearchitecture, at which layer, and with which fixture.

## Legend

Settled:

- Y: expected behavior is agreed; write a strict xfail (or test and fix now, if marked Now).
- P: part of the expectation is agreed; test only that part (named in Notes).
- N: blocked on a decision; no test yet.
- S: structural, no behavioral symptom of its own; enforce by lint or type gate.
- C: pending a scope cut; don't test a feature that may be removed.

Layer (lowest that observes the finding faithfully):

- U: in-process CLI call on a synthetic notebook, asserting on `--format json` output and the written manifest literal. Environment irrelevant or supplied by the fake PyPI.
- V: real environment. A session-scoped base venv with steady-py installed, plus per-test site directories holding hand-written dist-infos or locally built wheels; the CLI runs as a subprocess of that venv's Python. Runs on the Windows host.
- L: real kernel through `jupyter_client` in the base venv. Runs on the host; Docker is not required.
- D: Docker, only where the host can't reproduce the condition.

Now: a point fix that survives any architecture; write the test and fix together.

Reading test status:

- Only Y and P rows get tests. N, S and C rows have none by design.
- `pytest --findings` lists every test tagged with a matrix ID, one line each: ID, status, tier, test. A line means the test exists. Status `covered` means it passes; `known bug` means a strict xfail that fails until the bug is fixed, and fails the run if it starts passing. Tier says which `-m` run executes it (unit runs by default).
- A Y or P row with no lines in `--findings` has no test yet.
- The Status column holds only what markers can't express: `tested (see --findings)`, `deferred: <reason>` (listed under Deferred), or `not planned` for N, S and C rows, which get tests only once a decision settles them. Pass and xfail state is never recorded here; `--findings` owns it.
- `tests/characterization/` is the regression net for the rearchitecture: plain passing tests of current behavior at the public boundary. They carry no matrix IDs, so `--findings` doesn't list them.

## Foundations

1. F1 Notebook builder: cells in notebook order (markdown and code), magics, execution counts, writes a tmp `.ipynb`.
2. F2 Outcome helpers: parse the JSON report and the manifest literal (`ast.literal_eval`, independent of steady-py code); assertions such as `pins(name) == v`, `warning(kind, cell=n)`, `exit_code == 1`. Only these helpers know result shapes.
3. F3 Fake PyPI: a local HTTP server serving JSON API responses per project/version, with failure injection (disconnect mid-read, 404, timeout). Requires one code change: a configurable PyPI base URL (hardcoded `https://pypi.org` in `pypi.py`). A mirror setting is also a real user need (devpi, Artifactory), so the change isn't test-only.
4. F4 Dist-info writer: writes `METADATA`, `INSTALLER`, `RECORD`, `top_level.txt`, `direct_url.json` into a tmp site dir. Covers conda stubs, legacy versions, shadowing and namespace packages without pip or conda.
5. F5 Wheelhouse: tiny stub projects built into local wheels, plus cached build backends (setuptools, hatchling, pdm-backend, wheel) so editable installs run offline. Populated once from PyPI.
6. F6 Local git repo: `git+file://` sources, including a monorepo with subdirectories.
7. F7 Kernel runner: `jupyter_client` against the base venv's kernel.
8. F8 Docker scenarios: non-root user with an admin-owned Python or venv; a Miniconda image for a one-time confirmation of the conda rows.

## Parsing and notebook reading

| ID  | Finding                                                                                                                                                               | Settled | Status                                               | Layer | Fixture | Notes                                                                                                            |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- | ---------------------------------------------------- | ----- | ------- | ---------------------------------------------------------------------------------------------------------------- |
| K1  | One IPython line breaks manifest extraction; blanking corrupts valid Python                                                                                           | Y       | tested (see --findings)                              | U     | F1      | check and scan-delta on a notebook with `x = !ls`, `df?`, a triple-quote ending on a `%` line                    |
| K2  | Unparseable cell drops its imports silently                                                                                                                           | Y       | tested (see --findings)                              | U     | F1      | Expect imports found or a diagnostic naming the cell, never silence; a cell using syntax newer than the running interpreter (`lazy import`) gets a diagnostic naming both versions                                              |
| G1  | Guarded install line erases the cell's imports                                                                                                                        | Y       | tested (see --findings)                              | U     | F1      | Corpus: 14.6% of install lines in 3.10+ notebooks are guarded (3.6% in 3.7 and earlier) |
| G13 | `%%writefile` with leading blank lines scanned as code                                                                                                                | Y       | tested (see --findings)                              | U     | F1      |                                                                                                                  |
| G15 | Live kernel reads transformed source; no install lines harvested                                                                                                      | Y       | tested (see --findings)                              | L     | F7      | One cell per form: `%pip`, `!pip`, `%%writefile`, `%conda`; file mode is each test's control. Existing tests patch the reader and can't catch this |
| D1  | Live session counts `steady_py` as an import | Y       | tested (see --findings)                              | L     | F7      | Also in file mode: a scan of a snapshotted notebook reports Cell 2's `import steady_py` as a platform module (tested at U), and a user's own `import steady_py` in either mode |
| P7  | Batch notices lose their notebook; cell numbers match nothing visible                                                                                                 | Y       | tested (see --findings)                              | U     | F1      | Markdown cells before code; same notice in two notebooks                                                         |

## Install-line harvesting

| ID  | Finding                                                               | Settled | Status                                               | Layer | Fixture    | Notes                                                                                                                                       |
| --- | --------------------------------------------------------------------- | ------- | ---------------------------------------------------- | ----- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| G3  | Guarded install treated as top-level                                  | Y       | tested (see --findings)                              | U     | F1         | Per the guarded-installs decision: not pinned unconditionally, reported; Corpus: see G1 |
| G6  | Exclusive branches collapse to the last pin                           | Y       | tested (see --findings)                              | U     | F1         | Follows G3; one test file                                                                                                                   |
| G4  | `%pip install $pkg` becomes package `$pkg`                            | Y       | tested (see --findings)                              | U     | F1         | Warning, no package                                                                                                                         |
| G5  | Missed install forms                                                  | Y       | tested (see --findings)                              | U     | F1         | Parametrize: `python -m pip`, `{sys.executable} -m pip`, `os.system`, `subprocess` list, `get_ipython().system`, `%uv pip`, `conda run pip`; Corpus: the `subprocess` list form is 11.2% of install lines in 3.10+ notebooks |
| G11 | `--opt=value` flags dropped                                           | Y       | tested (see --findings)                              | U     | F1         |                                                                                                                                             |
| G12 | PEP 508 direct reference split into three entries                     | Y       | tested (see --findings)                              | U     | F1         | Unquoted form flagged                                                                                                                       |
| G14 | `--no-deps` and raw-install index flags dropped                       | Y       | tested (see --findings)                              | U     | F1         | Assert on manifest fields; Corpus: `--no-deps` on 11.5% of install lines in 3.10+ notebooks |
| G16 | Install line with extras produces an invalid pin                      | Y       | tested (see --findings)                              | V     | F4, F3     | Pair with R1                                                                                                                                |
| P4  | `-e path` vanishes with no warning                                    | Y       | tested (see --findings)                              | U     | F1         |                                                                                                                                             |
| C2  | mamba, micromamba, `conda env update` give no notice                  | Y       | tested (see --findings)                              | U     | F1         | `--file` handled like `-r`                                                                                                                  |
| D5  | Folder scan reports a package named `---`                             | Y       | tested (see --findings)                              | U     | magic_sink |                                                                                                                                             |
| G2  | Pip/import name mismatch drops guard; paddle double entry             | P       | tested (see --findings)                              | V     | F4         | Single entry settled; unconditional install vs guarded import precedence, python-dotenv does not double; only paddle doeopen                |
| G7  | Non-literal dynamic imports: generic warning                          | N       | not planned                                          |       |            | Behavior undefined                                                                                                                          |
| G8  | Guard tagging coarse                                                  | N       | not planned                                          |       |            | No agreed guard classes                                                                                                                     |
| G9  | Wrapper helper loses guard                                            | N       | not planned                                          |       |            | Follows the AST change                                                                                                                      |
| G17 | Non-canonical install name listed twice, plus a nameless header entry | Y       | tested (see --findings)                              | U     | F1         | D5 is its directory-scan symptom; also the nameless `%%writefile` header entry                                                                                                            |
| G18 | Trailing comment on an install line harvested as packages | Y | deferred: written with the install-line rework       | U | F1 | 99 corpus lines |
| G19 | Combined short flags (`-qr file`) hide `-r` | Y | deferred: written with the install-line rework       | U | F1 | 48 corpus lines |
| CH3 | PEP 508 `name @ url` stored as the bare URL | Y | deferred: written with step 1.4                      | U | F1 | Decided (decision 10): stored as written, `name @ url`; test alongside G12 |

## Environment capture

| ID  | Finding                                                                | Settled | Status                                               | Layer | Fixture              | Notes                                                                                    |
| --- | ---------------------------------------------------------------------- | ------- | ---------------------------------------------------- | ----- | -------------------- | ---------------------------------------------------------------------------------------- |
| E1  | Failed freeze gives an empty verified manifest, exit 0                 | Y       | tested (see --findings)                              | V     | `venv --without-pip` | Exit 2, nothing written. Non-ASCII editable path variant on the Windows host             |
| E2a | Local `file://` wheel dropped                                          | Y       | tested (see --findings)                              | V     | F5                   | Expect a creator candidate report                                                        |
| E2b | Conda-built `file://` packages dropped                                 | Y       | tested (see --findings)                              | V     | F4                   | `INSTALLER=conda`; confirm once in F8                                                    |
| E3  | Overlay pins a shadowed copy                                           | Y       | tested (see --findings)                              | V     | F4                   | Two site dirs, first without `direct_url.json`                                           |
| E5  | `#subdirectory=` lost; monorepo packages merged                        | Y       | tested (see --findings)                              | V     | F4                   | The loss is in reading `direct_url.json`; a runtime install from a monorepo needs F6     |
| E6  | `===` versions stored with a stray `=`                                 | Y       | tested (see --findings)                              | V     | F4                   | Can't be built with modern tools; hand-written only                                      |
| E7  | Directory scan reports git and conda packages as missing               | Y       | tested (see --findings)                              | V     | F4                   |                                                                                          |
| E8  | Cell 2 claims a direct reference is installed when nothing installs it | Y       | tested (see --findings)                              | V     | F4                   | One of few assertions on rendered text; a direct reference exists only in a venv         |
| K6  | OpenCV variant from `pip list`; shadowed variant pinned                | Y       | tested (see --findings)                              | V     | F4                   | cv2 stubs in two site dirs                                                               |
| C1  | conda-forge OpenCV stubs: wrong variant, false removed                 | Y       | tested (see --findings)                              | V     | F4, F3               |                                                                                          |
| K12 | Live kernel treats site-packages modules as local                      | Y       | tested (see --findings)                              | L     | F7                   | The live path is the Kaggle path, and 82% of Kaggle notebooks install nothing, so this drops nearly every pin |
| E4  | `--full-freeze` embeds private paths                                   | C       | not planned                                          |       |                      | Pending removal decision                                                                 |
| DG5 | Pins come from the CLI's interpreter (pipx, uv tool)                   | N       | not planned                                          | V     | F5                   | Characterization test to confirm the lead; expected behavior is an architecture decision |

## Editables and local modules

| ID  | Finding                                                      | Settled | Status                                               | Layer | Fixture    | Notes                                                        |
| --- | ------------------------------------------------------------ | ------- | ---------------------------------------------------- | ----- | ---------- | ------------------------------------------------------------ |
| ED1 | Editable's own dependencies never pinned                     | Y       | deferred: venv layer, written with the rearchitecture | V     | F5         |                                                              |
| ED2 | Directory and file runs classify the same import differently | Y       | deferred: venv layer, written with the rearchitecture | V     | F5         | Also covers test_plan item 3 (file and directory runs agree) |
| ED3 | Editable import: no warning, exit 0                          | Y       | tested (see --findings)                              | V     | F5         |                                                              |
| ED4 | Join misses hatchling, pdm, legacy editables                 | Y       | deferred: venv layer, written with the rearchitecture | V     | F5         | `conda develop` confirmed once in F8                         |
| ED5 | VCS editable reported as a local path                        | Y       | deferred: needs F6                                   | V     | F5, F6     |                                                              |
| ED6 | Data folder hides an installed package                       | Y       | deferred: venv layer, written with the rearchitecture | V     | F4         | `datasets/` holding only a CSV                               |
| K4  | `importlib.machinery` bound only incidentally                | Y       | tested (see --findings)                              | U     | lint       | Now; fixed. Structural: no behavioral symptom while it's latent |

## Resolution and pins

| ID  | Finding                                                     | Settled | Status                                               | Layer | Fixture | Notes                                                             |
| --- | ----------------------------------------------------------- | ------- | ---------------------------------------------------- | ----- | ------- | ----------------------------------------------------------------- |
| P1  | Version operators stripped into invalid pins                | Y       | tested (see --findings)                              | U     | F1      | Host `packaging` is the installed version                         |
| P2  | Namespace import resolves to an arbitrary distribution      | Y       | tested (see --findings)                              | V     | F4      | Needs `RECORD` for `dist.files`                                   |
| P6  | Not-found imports labeled `pinned`                          | Y       | tested (see --findings)                              | U     | F1      | JSON status                                                       |
| LV4 | Cell 1 lists flag names as URLs                             | Y       | tested (see --findings)                              | U     | F1      |                                                                   |
| K9  | Extras promotion nondeterministic                           | P       | tested (see --findings)                              | V     | F4      | Determinism settled (four `PYTHONHASHSEED` values); heuristic open |
| CI2 | Environment version paired with an unversioned line's index | P       | tested (see --findings)                              | U     | F1      | Warning settled; pairing rule open                                |
| K10 | Display text parsed back as data                            | S       | not planned                                          |       |         | Visible effect covered by E7                                      |
| G10 | `--universal` duplicates resolution                         | C       | not planned                                          |       |         |                                                                   |
| LV3 | `--universal` can't install `+cu` builds                    | C       | not planned                                          |       |         |                                                                   |

## Validation

| ID  | Finding                                                        | Settled | Status                                               | Layer | Fixture | Notes                                                                      |
| --- | -------------------------------------------------------------- | ------- | ---------------------------------------------------- | ----- | ------- | -------------------------------------------------------------------------- |
| K3  | Dropped connection crashes the run                             | Y       | tested (see --findings)                              | U     | F3      | Now; fixed                                                                 |
| K5  | Transitive markers use the host platform                       | N       | not planned                                          |       |         | Fires only when check runs on a different OS than snapshot; see decision 6 |
| K8  | Lookup failure reported as a confirmed conflict                | Y       | tested (see --findings)                              | U     | F3      |                                                                            |
| LV1 | Local-version pins skip every PyPI check                       | Y       | tested (see --findings)                              | U     | F3      | Include an old baseline: not_checked_at_generation, not new                |
| CI1 | Custom-index project on PyPI reported removed; graph abandoned | Y       | tested (see --findings)                              | U     | F3      | Index lookup through the simple API deferred until a fake index exists. A removed pin also gets a duplicate `conflict` on itself |
| LV2 | `custom_sourced` decided by the wrong rule                     | P       | tested (see --findings)                              | U     | F3      | Network-error case settled; build-tag field and runtime message open (DG6) |
| CH1 | A manifest with an unknown or missing required field can't be read (exit 2) | N | not planned                                          |  |  | Found in the characterization pass: an older steady-py can't read a newer manifest; see decision 9 |

## Cell 2 runtime

| ID  | Finding                                             | Settled | Status                                               | Layer | Fixture | Notes                                       |
| --- | --------------------------------------------------- | ------- | ---------------------------------------------------- | ----- | ------- | ------------------------------------------- |
| R1  | Extras pins never pass the installed check          | Y       | tested (see --findings)                              | V     | F5      | Written wheels                              |
| R2  | Pin that drifts after verification still counts     | Y       | tested (see --findings)                              | V     | F5      | Installing B moves A                        |
| R3  | String equality instead of PEP 440                  | Y       | tested (see --findings)                              | V     | F5      | `demo-loc 1.0+cu126` installed, pin `==1.0` |
| D6  | Troubleshooting advice drops the version            | Y       | tested (see --findings)                              | V     | F5      | Failing wheel                               |
| R4  | User-site install counts as verified                | Y       | tested (see --findings)                              | D     | F8      | Non-root user, root-owned Python            |
| R5  | Helper bootstrap succeeds, import fails or is older | Y       | tested (see --findings)                              | D     | F8      | Cases (a) and (b)                           |
| D2  | Cell 2 silently uses another steady-py version      | Y       | tested (see --findings)                              | D     | F8      | Same scenario as R5                         |
| D3  | Same advice for every failure                       | N       | not planned                                          |       |         | Needs a design pass                         |

## Writing and output

| ID  | Finding                                          | Settled | Status                                               | Layer | Fixture | Notes                                                                     |
| --- | ------------------------------------------------ | ------- | ---------------------------------------------------- | ----- | ------- | ------------------------------------------------------------------------- |
| DR1 | Venvs not named venv are scanned and rewritten   | Y       | tested (see --findings)                              | U     | F1      | Dirs with `pyvenv.cfg`, `conda-meta/`, `site-packages`. Now; fixed           |
| K7  | Prior-setup-cell match discards user code        | Y       | tested (see --findings)                              | U     | F1      | Warning expected                                                          |
| P3  | Delta ignores flags, raw installs, local modules | Y       | tested (see --findings)                              | U     | F1      | Per manifest-updating item 6                                              |
| CH2 | Delta matches package names without normalizing them | N | not planned                                          |  |  | Found in the characterization pass: `Packaging` vs `packaging` at one version is removed plus added; classify (decision 10) |
| D4  | Single-file text scan fails                      | Y       | tested (see --findings)                              | U     | F1      |                                                                           |
| P5  | Unchanged notebook rewritten                     | P       | tested (see --findings)                              | U     | F1      | Cell ID reuse settled; `generated_at` open                                |
| K11 | Setup markdown matched by heading text           | N       | not planned                                          |       |         | No fix agreed                                                             |

## Code health

| ID  | Finding                                 | Settled | Status                                               | Layer | Notes                                                       |
| --- | --------------------------------------- | ------- | ---------------------------------------------------- | ----- | ----------------------------------------------------------- |
| H3  | Logger never propagates as a library    | Y       | tested (see --findings)                              | U     | Now; fixed                                                  |
| H1  | `Any` parameters, tuple-unpacking shims | S       | not planned                                          |       | `mypy --strict` gate on touched modules                     |
| H2  | Broad `except Exception`                | S       | not planned                                          |       | Ruff `BLE001` gate with an allowlist for third-party probes |

## Design gaps (no tests yet)

DG1 optional-dependency candidates, DG2 guarded-alternative reporting, DG3 platform baseline, DG4 guard status in the manifest, DG6 build-tag runtime policy, DG7 creator install lines undoing Cell 2: all N, blocked on feature design.

## Totals

86 rows: 60 Y, 5 P, 15 N (including the six design gaps and three rows from the characterization pass), 3 S, 3 C.

Tests exist for 57 of the 65 Y and P rows; the other eight are listed under Deferred.

By layer (Y and P, 65 rows): 35 U, 24 V, 3 L, 3 D. Docker is needed for three runtime rows plus a one-time conda confirmation.

Now: K3, K4, H3, DR1, all fixed.

## Deferred

Rows:

1. G18, G19 (U): install-line tokenizing. Written with the install-line rework, so the tests and the parser change arrive together.
2. ED1, ED2, ED4, ED6 (V): the editable and local-module join is rewritten in the rearchitecture. They need stub projects not yet in `tests/fixtures/projects/` (hatchling, pdm-backend, a legacy `setup.py develop`), and ED4 needs design §13.1 confirmed.
3. ED5 (V): also needs F6.
4. CH3 (U): written with step 1.4's argument parsing, alongside G12.

Foundations and infrastructure:

1. F6 local git repositories (`tests/support/gitrepo.py`, design §9): not built. First needed by ED5; also unlocks a real `git+file://` raw install at run time and E5's runtime monorepo install. Windows URL form still to verify (design §13.2).
2. Miniconda image (F8, design §11.3): a one-time confirmation that the hand-written conda dist-infos behind E2b, C1 and ED4's `conda develop` match real conda. The rows are already tested against the stubs, so this checks the fixture, not the product.
3. Runner conversion (design §11.3): `tests/runners/` becoming pytest-collected `docker` tests, with `run_suite.py` a thin driver. The runners are the only coverage of the Kaggle and Colab images; converting them before the rearchitecture settles Cell 2's output would mean converting their assertions twice.
4. `e2e_harness.py`'s own kernel manager (design §10.4): replaced by `tests/support/kernel.py` as part of the runner conversion.

## Decisions that unblock rows

1. Scope cuts for `--universal` and `--full-freeze` (G10, LV3, E4).
2. Guard precedence when an unconditional install meets a guarded import (G2).
3. What `generated_at` records (P5).
4. Build-tag runtime policy and manifest field (LV2, DG6).
5. Whether the CLI describes its own interpreter or the kernel's (DG5).
6. Whether the manifest records the target platform (K5). The manifest has no platform field, so check can't know it; a field adds complexity for a case that arises only when check runs on a different OS than snapshot (a Colab repository checked from a laptop, say). Revisit if that becomes a supported workflow.
7. Whether DG1 (undeclared optional dependencies) becomes a settled row. Corpus: where the trigger appears, the dependency is undeclared in nearly every notebook (Styler without jinja2 344 of 344, parquet without pyarrow 217 of 227, Excel without an engine 112 of 130). The platform image hides it, so the notebook fails only once it leaves Kaggle or Colab. Open: report as candidates, or pin what's installed.
8. The exit code for a heuristic finding classified `not_checked_at_generation`. Today it exits 1 (only `known` heuristics are exempt), and the characterization tests pin that; the comment in LV1's test says a newly visible finding on an old notebook must not start exiting 1. If the comment is the rule, this is a finding and the characterization test flips with the fix.
9. Manifest schema compatibility (CH1): whether a reader tolerates unknown fields, and what a missing field means.
10. Classify CH2. CH3 is decided: `name @ url` is stored as written.
