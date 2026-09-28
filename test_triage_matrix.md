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

| ID  | Finding                                                                                                                                                               | Settled | Layer | Fixture | Notes                                                                                                            |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- | ----- | ------- | ---------------------------------------------------------------------------------------------------------------- |
| K1  | One IPython line breaks manifest extraction; blanking corrupts valid Python                                                                                           | Y       | U     | F1      | check and scan-delta on a notebook with `x = !ls`, `df?`, a triple-quote ending on a `%` line                    |
| K2  | Unparseable cell drops its imports silently                                                                                                                           | Y       | U     | F1      | Expect imports found or a diagnostic naming the cell, never silence                                              |
| G1  | Guarded install line erases the cell's imports                                                                                                                        | Y       | U     | F1      |                                                                                                                  |
| G13 | `%%writefile` with leading blank lines scanned as code                                                                                                                | Y       | U     | F1      |                                                                                                                  |
| G15 | Live kernel reads transformed source; no install lines harvested                                                                                                      | Y       | L     | F7      | One cell per form: `%pip`, `!pip`, `%%writefile`, `%conda`. Existing tests patch the reader and can't catch this |
| D1  | Live session counts `steady_py` as an import Also in file mode: a scan of a snapshotted notebook reports Cell 2's import steady_py as a platform module (tested at U) | Y       | L     | F7      |                                                                                                                  |
| P7  | Batch notices lose their notebook; cell numbers match nothing visible                                                                                                 | Y       | U     | F1      | Markdown cells before code; same notice in two notebooks                                                         |

## Install-line harvesting

| ID  | Finding                                                               | Settled | Layer | Fixture    | Notes                                                                                                                                       |
| --- | --------------------------------------------------------------------- | ------- | ----- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| G3  | Guarded install treated as top-level                                  | Y       | U     | F1         | Per the guarded-installs decision: not pinned unconditionally, reported                                                                     |
| G6  | Exclusive branches collapse to the last pin                           | Y       | U     | F1         | Follows G3; one test file                                                                                                                   |
| G4  | `%pip install $pkg` becomes package `$pkg`                            | Y       | U     | F1         | Warning, no package                                                                                                                         |
| G5  | Missed install forms                                                  | Y       | U     | F1         | Parametrize: `python -m pip`, `{sys.executable} -m pip`, `os.system`, `subprocess` list, `get_ipython().system`, `%uv pip`, `conda run pip` |
| G11 | `--opt=value` flags dropped                                           | Y       | U     | F1         |                                                                                                                                             |
| G12 | PEP 508 direct reference split into three entries                     | Y       | U     | F1         | Unquoted form flagged                                                                                                                       |
| G14 | `--no-deps` and raw-install index flags dropped                       | Y       | U     | F1         | Assert on manifest fields                                                                                                                   |
| G16 | Install line with extras produces an invalid pin                      | Y       | V     | F4, F3     | Pair with R1                                                                                                                                |
| P4  | `-e path` vanishes with no warning                                    | Y       | U     | F1         |                                                                                                                                             |
| C2  | mamba, micromamba, `conda env update` give no notice                  | Y       | U     | F1         | `--file` handled like `-r`                                                                                                                  |
| D5  | Folder scan reports a package named `---`                             | Y       | U     | magic_sink |                                                                                                                                             |
| G2  | Pip/import name mismatch drops guard; paddle double entry             | P       | V     | F4         | Single entry settled; unconditional install vs guarded import precedence, python-dotenv does not double; only paddle doeopen                |
| G7  | Non-literal dynamic imports: generic warning                          | N       |       |            | Behavior undefined                                                                                                                          |
| G8  | Guard tagging coarse                                                  | N       |       |            | No agreed guard classes                                                                                                                     |
| G9  | Wrapper helper loses guard                                            | N       |       |            | Follows the AST change                                                                                                                      |
| G17 | Non-canonical install name listed twice, plus a nameless header entry | Y       | U     | F1         | D5 is its directory-scan symptom                                                                                                            |

## Environment capture

| ID  | Finding                                                                | Settled | Layer | Fixture              | Notes                                                                                    |
| --- | ---------------------------------------------------------------------- | ------- | ----- | -------------------- | ---------------------------------------------------------------------------------------- |
| E1  | Failed freeze gives an empty verified manifest, exit 0                 | Y       | V     | `venv --without-pip` | Exit 2, nothing written. Non-ASCII editable path variant on the Windows host             |
| E2a | Local `file://` wheel dropped                                          | Y       | V     | F5                   | Expect a creator candidate report                                                        |
| E2b | Conda-built `file://` packages dropped                                 | Y       | V     | F4                   | `INSTALLER=conda`; confirm once in F8                                                    |
| E3  | Overlay pins a shadowed copy                                           | Y       | V     | F4                   | Two site dirs, first without `direct_url.json`                                           |
| E5  | `#subdirectory=` lost; monorepo packages merged                        | Y       | V     | F6                   |                                                                                          |
| E6  | `===` versions stored with a stray `=`                                 | Y       | V     | F4                   | Can't be built with modern tools; hand-written only                                      |
| E7  | Directory scan reports git and conda packages as missing               | Y       | V     | F4                   |                                                                                          |
| E8  | Cell 2 claims a direct reference is installed when nothing installs it | Y       | U     | F1                   | One of few assertions on rendered text                                                   |
| K6  | OpenCV variant from `pip list`; shadowed variant pinned                | Y       | V     | F4                   | cv2 stubs in two site dirs                                                               |
| C1  | conda-forge OpenCV stubs: wrong variant, false removed                 | Y       | V     | F4, F3               |                                                                                          |
| K12 | Live kernel treats site-packages modules as local                      | Y       | L     | F7                   |                                                                                          |
| E4  | `--full-freeze` embeds private paths                                   | C       |       |                      | Pending removal decision                                                                 |
| DG5 | Pins come from the CLI's interpreter (pipx, uv tool)                   | N       | V     | F5                   | Characterization test to confirm the lead; expected behavior is an architecture decision |

## Editables and local modules

| ID  | Finding                                                      | Settled | Layer | Fixture    | Notes                                                        |
| --- | ------------------------------------------------------------ | ------- | ----- | ---------- | ------------------------------------------------------------ |
| ED1 | Editable's own dependencies never pinned                     | Y       | V     | F5         |                                                              |
| ED2 | Directory and file runs classify the same import differently | Y       | V     | F5         | Also covers test_plan item 3 (file and directory runs agree) |
| ED3 | Editable import: no warning, exit 0                          | Y       | V     | F5         |                                                              |
| ED4 | Join misses hatchling, pdm, legacy editables                 | Y       | V     | F5         | `conda develop` confirmed once in F8                         |
| ED5 | VCS editable reported as a local path                        | Y       | V     | F5, F6     |                                                              |
| ED6 | Data folder hides an installed package                       | Y       | V     | F4         | `datasets/` holding only a CSV                               |
| K4  | `importlib.machinery` bound only incidentally                | Y       | U     | subprocess | Now                                                          |

## Resolution and pins

| ID  | Finding                                                     | Settled | Layer | Fixture | Notes                                                             |
| --- | ----------------------------------------------------------- | ------- | ----- | ------- | ----------------------------------------------------------------- |
| P1  | Version operators stripped into invalid pins                | Y       | V     | F4      |                                                                   |
| P2  | Namespace import resolves to an arbitrary distribution      | Y       | V     | F4      | Needs `RECORD` for `dist.files`                                   |
| P6  | Not-found imports labeled `pinned`                          | Y       | U     | F1      | JSON status                                                       |
| LV4 | Cell 1 lists flag names as URLs                             | Y       | U     | F1      |                                                                   |
| K9  | Extras promotion nondeterministic                           | P       | V     | F4      | Determinism settled (two `PYTHONHASHSEED` values); heuristic open |
| CI2 | Environment version paired with an unversioned line's index | P       | V     | F4      | Warning settled; pairing rule open                                |
| K10 | Display text parsed back as data                            | S       |       |         | Visible effect covered by E7                                      |
| G10 | `--universal` duplicates resolution                         | C       |       |         |                                                                   |
| LV3 | `--universal` can't install `+cu` builds                    | C       |       |         |                                                                   |

## Validation

| ID  | Finding                                                        | Settled | Layer | Fixture | Notes                                                                      |
| --- | -------------------------------------------------------------- | ------- | ----- | ------- | -------------------------------------------------------------------------- |
| K3  | Dropped connection crashes the run                             | Y       | U     | F3      | Now                                                                        |
| K5  | Transitive markers use the host platform                       | Y       | U     | F3      | Linux-marked dependencies, run on the Windows host                         |
| K8  | Lookup failure reported as a confirmed conflict                | Y       | U     | F3      |                                                                            |
| LV1 | Local-version pins skip every PyPI check                       | Y       | U     | F3      | Include an old baseline: not_checked_at_generation, not new                |
| CI1 | Custom-index project on PyPI reported removed; graph abandoned | Y       | U     | F3      | Index lookup through the simple API deferred until a fake index exists     |
| LV2 | `custom_sourced` decided by the wrong rule                     | P       | U     | F3      | Network-error case settled; build-tag field and runtime message open (DG6) |

## Cell 2 runtime

| ID  | Finding                                             | Settled | Layer | Fixture | Notes                                       |
| --- | --------------------------------------------------- | ------- | ----- | ------- | ------------------------------------------- |
| R1  | Extras pins never pass the installed check          | Y       | V     | F5      |                                             |
| R2  | Pin that drifts after verification still counts     | Y       | V     | F5      | Installing B moves A                        |
| R3  | String equality instead of PEP 440                  | Y       | V     | F5      | `demo-loc 1.0+cu126` installed, pin `==1.0` |
| D6  | Troubleshooting advice drops the version            | Y       | V     | F5      | Failing wheel                               |
| R4  | User-site install counts as verified                | Y       | D     | F8      |                                             |
| R5  | Helper bootstrap succeeds, import fails or is older | Y       | D     | F8      | Cases (a) and (b)                           |
| D2  | Cell 2 silently uses another steady-py version      | Y       | D     | F8      | Same scenario as R5                         |
| D3  | Same advice for every failure                       | N       |       |         | Needs a design pass                         |

## Writing and output

| ID  | Finding                                          | Settled | Layer | Fixture | Notes                                                                     |
| --- | ------------------------------------------------ | ------- | ----- | ------- | ------------------------------------------------------------------------- |
| DR1 | Venvs not named venv are scanned and rewritten   | Y       | U     | F1      | Dirs with `pyvenv.cfg`, `conda-meta/`, `site-packages`. Now (destructive) |
| K7  | Prior-setup-cell match discards user code        | Y       | U     | F1      | Warning expected                                                          |
| P3  | Delta ignores flags, raw installs, local modules | Y       | U     | F1      | Per manifest-updating item 6                                              |
| D4  | Single-file text scan fails                      | Y       | U     | F1      |                                                                           |
| P5  | Unchanged notebook rewritten                     | P       | U     | F1      | Cell ID reuse settled; `generated_at` open                                |
| K11 | Setup markdown matched by heading text           | N       |       |         | No fix agreed                                                             |

## Code health

| ID  | Finding                                 | Settled | Layer | Notes                                                       |
| --- | --------------------------------------- | ------- | ----- | ----------------------------------------------------------- |
| H3  | Logger never propagates as a library    | Y       | U     | Now                                                         |
| H1  | `Any` parameters, tuple-unpacking shims | S       |       | `mypy --strict` gate on touched modules                     |
| H2  | Broad `except Exception`                | S       |       | Ruff `BLE001` gate with an allowlist for third-party probes |

## Design gaps (no tests yet)

DG1 optional-dependency candidates, DG2 guarded-alternative reporting, DG3 platform baseline, DG4 guard status in the manifest, DG6 build-tag runtime policy, DG7 creator install lines undoing Cell 2: all N, blocked on feature design.

## Totals

80 rows: 57 Y, 5 P, 12 N, 3 S, 3 C.

By layer (Y and P, 62 rows): 31 U, 25 V, 3 L, 3 D. Docker is needed for three runtime rows plus a one-time conda confirmation.

Now: K3, K4, H3, DR1.

## Decisions that unblock rows

1. Scope cuts for `--universal` and `--full-freeze` (G10, LV3, E4).
2. Guard precedence when an unconditional install meets a guarded import (G2).
3. What `generated_at` records (P5).
4. Build-tag runtime policy and manifest field (LV2, DG6).
5. Whether the CLI describes its own interpreter or the kernel's (DG5).
