# Fix plan

The work between the verified review findings and the first release: how each step fixes its findings, and when. How the work is done is in working_rules.md; what is wrong and why is in review_findings.md; each row's ID, status, tests and step are in test_triage_matrix.md. Where the test documents say "the rearchitecture", they mean this plan.

## 1. Approach

1. Fix the findings, consolidate each duplicated decision into one owner, and rework the subsystems that are fundamentally flawed. There is no global redesign; anything broader than a finding needs a stated reason.
2. The release gate is two features specified in review_findings.md, Proposed features: install-line reconciliation with creator declarations, and snapshot manifest updating. Their prerequisites set the order of the steps.
3. There is no up-front inventory of every decision. Each step starts by finding every place in the code that makes the decisions it changes.

## 2. Scope

In scope:

1. Every matrix row with a step or point fix in its Step column; N rows among them once their decision is made (section 7).
2. S rows (Step `touched`) only on code a step touches, not as a sweep.
3. Two items from development.md's open design questions: atomic `--in-place` writes, and removing only cells the tool can prove it owns (the managed tag, or a manifest whose hash still validates). The second also resolves K11.
4. Removing the `scan` verb (section 5, step 3).

Out of scope (Step `out` in the matrix), with the reasons:

1. G10, LV3, E4 (`--universal`, `--full-freeze`), pending the scope cut (decision 1).
2. K5 (transitive markers evaluated on the host platform).
3. G7 and G8, which keep the generic warnings they give today.
4. DG1 and DG3, automatic detection of optional dependencies and the platform baseline it needs. Declarations let a creator add optional dependencies by hand. The corpus evidence makes this a strong first feature after release.
5. Recording transitive dependency versions. steady-py pins what a notebook imports and installs and warns on likely problems through check; it does not lock the whole environment.
6. The remaining open design questions in development.md.
7. G20, rare install-line forms read differently from pip; revisited if the corpus shows them.

## 3. Contracts

Standards for everything else are in working_rules.md, section 1. Two structures are defined before the step that builds them, because they are expensive to change later. The install-line record's fields are settled; the manifest's are set at the start of step 3 and must carry at least the following.

1. The install-line record (step 1), one per install command, produced once and consumed by snapshot, check and reconciliation:
   1. Text: the logical line as written, continuations joined; for a Python call form, the call as `ast.unparse` renders it (normalized quoting and spacing).
   2. Location: the notebook path, the cell's 1-based position in notebook order (markdown cells counted), the line within the cell, the execution count, and the nearest preceding heading. In live mode the position and heading are absent and the execution count is present.
   3. Tool: pip, uv, conda-family (conda, mamba, micromamba) or system (apt-get, brew, yum). Conda and system lines are recorded; their targets are parsed no further than names.
   4. Invocation: line magic, shell escape (`!`), shell cell (`%%bash`, `%%sh`), or Python call (`subprocess` list, `os.system`, `get_ipython().system`). `python -m pip`, `{sys.executable} -m pip` and `conda run pip` are an invocation plus a tool, not separate forms.
   5. Kernel target: whether the line installs into the kernel's environment, and why not when it doesn't (`-t`/`--target`, `--prefix`, `--root`, conda `-n` naming another environment).
   6. Guard: none; a Python guard with its kind (if, try, except ImportError, function body) and condition text; or a shell guard, joined (`&&`, `||`) or conditional. Guarded lines carry a guard group and branch index, so mutually exclusive branches are identifiable.
   7. Readability: literal, computed (`$pkg`, `{var}`, non-literal call arguments) or unreadable, each with its diagnostic.
   8. Targets, each with its kind: a requirement parsed as pip parses it (name as typed and canonical, extras, specifier, marker; `name @ url` stored as written), a bare direct or VCS URL, a local path or wheel, an editable, or a requirements or constraints file (recorded, not read).
   9. Options: canonical name and value pairs (`-i x` and `--index-url=x` give the same pair), unknown options kept as written. A pip command line's options apply to every requirement on it, so they are stored once per line and each target inherits its line's options.
2. The manifest (step 3):
   1. Every field reported in the delta (P3).
   2. Provenance for each value: declaration, notebook text, environment, or carried forward from the previous manifest.
   3. Declarations with their provenance, and guard status as a protected field (DG4).
   4. A compatibility policy (decision 9) before the first release, since the release fixes the format. No manifest exists outside development yet, so the format can change freely until then.

## 4. Risks and how they are resolved

Corpus evidence for these resolutions is in review_findings.md, Corpus evidence.

1. Magic handling. Resolved: use IPython's transform (a standard library, with a loose lower bound so installing steady-py never moves a kernel's IPython). Cells that fail either way get K2's diagnostic.
2. Live mode sees only executed cells, in execution order, without cell positions. Accepted: reconciliation gives the best information available, locating lines by their text, with positions as hints where they exist.
3. Shell guards. Resolved: every shell-joined (`&&`, `||`) or shell-conditional install line counts as guarded and gets a warning, never delete advice.
4. Installed-state capture changing from `pip freeze` to `importlib.metadata`. Resolved: run both side by side (working_rules.md §2.4) and switch only once every disagreement is understood. Each method is expected to have its own quirks.
5. Pins taken from the wrong interpreter (DG5), when the CLI runs under pipx, `uv tool` or conda base while the kernel uses another environment. Resolved for the release: snapshot warns when most of a notebook's third-party imports aren't installed in the CLI's interpreter, naming it. A `--python` option like pip's can follow the release. Live mode can't hit this.
6. Harvesting regressions on patterns no test anticipated. Resolved: a before/after corpus scan for every harvesting change.
7. Reading install lines differently from the user's pip. steady-py's own dependencies can be stricter or newer than the pip in a notebook's kernel (`packaging` 26 rejects `x==0.0.0.nonexistent`, which pip still reads; pip adds install options). Resolved: install lines are read as pip reads them, not as the strictest library would; the pip option table is checked against the installed pip by a unit test; and the dev environment is pinned (step 2), so a run means the same thing on every machine.

## 5. Steps

Each step runs in its own context, ends with a handoff, and is reviewed before the next begins. Steps 1 and 2 are independent; 3 to 5 depend on them. Each step's done criterion: its rows' known-bug tests pass (the strict xfails flip to passing tests, and deferred rows get their tests in the step), the characterization tests still pass or changed with a stated reason, the unit and touched tiers pass, and harvesting changes have a clean corpus diff.

1. Install-line pipeline. Done: one reader, IPython's transform and one AST, one guard rule, and pip-style argument parsing, producing the install-line record of section 3.1.
2. Installed-state capture and the import join. A setup task comes first:
   1. A dev lock: a committed `constraints-dev.txt` at the repository root replaces `docs/requirements.txt`, covering pip, mypy and every test dependency, and the unit, venv and kernel tiers run against it. The Docker tiers stay unpinned, since they stand in for users' environments. `pyproject.toml` keeps its ranges.
   1. Capture from `importlib.metadata`, run side by side with `pip freeze` first: one source, first match on `sys.path` wins, versions always carried (including for direct references), PEP 610 data read directly. The differential test covers the Docker tiers (plain 3.11, Kaggle, Colab), a conda env and a uv env, and direct references (VCS with subdirectory, archives with hashes, conda-built `file://`, editables), since the reader reimplements pip's `direct_url_as_pep440_direct_reference`. Editable divergences to cover: PEP 660 editables agree apart from `direct_url.json` key order; legacy `setup.py develop` is detected through `.egg-link`/`easy-install.pth`; a VCS editable freezes as `-e git+...@<HEAD>` while `direct_url.json` holds a local `file://` URL; `conda develop` is visible only to `find_spec`; `packages_distributions()` returns duplicates, so dedupe before diffing. Freeze stays afterward as a test oracle only. Snapshot warns when most of a notebook's third-party imports aren't installed in the CLI's interpreter (section 4, item 5).
   2. One `root_dir` derivation for every entry point, and one import-to-distribution join: every import (normal cell, `%%writefile`, auxiliary tool) resolves through one helper, `packages_distributions()` first with `IMPORT_TO_PYPI_MAP` as a fallback, then the `find_spec` origin matched against editable source directories, with local-module classification only after distribution evidence is exhausted. Guards and submodules carry across pip-name and import-name differences. check's missing-root-module message gains a `--root-dir` hint.
3. Manifest model. Removes the `scan` verb, whose delta becomes snapshot's (snapshot without a write flag already writes nothing); the scan-specific tests go with it, and README and HELP drop it.
4. Declarations and install-line reconciliation, per review_findings.md, Proposed features.
5. Snapshot manifest updating, per review_findings.md, Proposed features.

## 6. Point fixes

Not on the features' path; each is done when convenient, in any step.

1. Cell 2 runtime (pf1).
2. Validation (pf2): one shared "is this pin from PyPI" predicate. A pin is non-PyPI if it carries an index flag, a local tag, or a version absent from PyPI; every site (`run_pin_checks`, `check_yanked_or_removed`, `resolve_transitive_graph`, `classify_against_baseline`, `custom_sourced`, `runtime.install`) uses it. Roots are excluded per version, not per project, so one bad pin never abandons the graph. At snapshot and at check, each flagged pin is looked up on its recorded index through the simple API (PEP 691/503).
3. Resolution and output (pf3).
4. Output safety (pf4), including atomic `--in-place` writes and ownership-proven cell removal (section 2).

## 7. Decisions and when they are needed

Numbers refer to the matrix's "Decisions that unblock rows", which owns each decision's content.

1. Before step 2.1: none open.
2. Before step 2.2: decision 2.
3. Before step 3: decisions 9 and 10.
4. Before step 4: decision 8, and the install-lines spec's open decisions 10.1 to 10.4.
5. Before step 5: the manifest-updating open items (baseline, GPU record, `custom_sourced` and `local_modules` under the update rule; `generated_at` and the hash when nothing changed; update and rebuild with `--in-place`).
6. With the runtime point fixes: decision 4.
7. Any time: decision 1.

## 8. After the plan, before release

1. development.md tasks 22 (review user messaging), 23 (hand-test every mode) and 25 (license).
2. A FAQ: steady-py beside Kaggle's "Pin to original environment" and beside `pip freeze`, what steady-py pins and what it doesn't (transitive dependencies), and the workflow: snapshot where the notebook runs, check from anywhere.
3. Bring development.md's Testing section up to date, or point it to working_rules.md and test_foundations_design.md.
