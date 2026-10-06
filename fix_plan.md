# Fix plan

The work between the verified review findings and the first release. How the work is done is in working_rules.md; the findings are in review_findings.md, and their IDs, tests and status in test_triage_matrix.md. Where the test documents say "the rearchitecture", they mean this plan.

## 1. Approach

1. Fix the findings, consolidate each duplicated decision into one owner, and rework the subsystems that are fundamentally flawed. There is no global redesign; anything broader than a finding needs a stated reason.
2. The release gate is two features specified in review_findings.md, Proposed features: install-line reconciliation with creator declarations, and snapshot manifest updating. Their prerequisites set the order of the steps.
3. There is no up-front inventory of every decision. Each step starts by finding every place in the code that makes the decisions it changes.

## 2. Scope

In scope:

1. Every Y and P row in the matrix, assigned to a step in section 5 or to the point fixes in section 6.
2. N rows once their decision is made (section 7).
3. S rows (H1, H2, K10) only on code a step touches, not as a sweep.
4. D3 and DG6, with decision 4, alongside the Cell 2 runtime fixes.
5. Two items from development.md's open design questions: atomic `--in-place` writes, and removing only cells the tool can prove it owns (the managed tag, or a manifest whose hash still validates). The second also resolves K11.
6. Removing the `scan` verb (section 5, step 3).

Out of scope:

1. G10, LV3, E4 (`--universal`, `--full-freeze`), pending the scope cut (decision 1).
2. K5 (transitive markers evaluated on the host platform).
3. G7 and G8, which keep the generic warnings they give today.
4. DG1 and DG3, automatic detection of optional dependencies and the platform baseline it needs. Declarations let a creator add optional dependencies by hand. Corpus evidence makes this a strong first feature after release (for example, Styler without jinja2 in 344 of 344 notebooks).
5. Recording transitive dependency versions. steady-py pins what a notebook imports and installs and warns on likely problems through check; it does not lock the whole environment.
6. The remaining open design questions in development.md.

Already fixed: K3, K4, H3, DR1.

## 3. Contracts

Standards for everything else are in working_rules.md, section 1. Two structures are defined before the step that builds them, because they are expensive to change later. Their exact fields are set at the start of that step; each must carry at least the following.

1. The install-line record (step 1), produced once and consumed by snapshot, check and reconciliation:
   1. The line's text as written, the notebook, and its position: the cell's position in the notebook and the nearest heading. In live mode, positions come from execution history and may be absent.
   2. Its form (`%pip`, `!pip`, `subprocess` list, and so on) and whether it installs into the kernel (`-t`, `--target` and remote sandboxes don't).
   3. Its guard state: none, a Python guard (and which kind), or shell-joined or shell-conditional.
   4. Its requirements, parsed as pip parses them (name, extras, specifier or direct URL), and its flags with their values and the requirements they apply to.
   5. Whether it could be read literally, or is computed (`$pkg`) or unreadable, with a diagnostic.
2. The manifest (step 3):
   1. Every field reported in the delta (P3).
   2. Provenance for each value: declaration, notebook text, environment, or carried forward from the previous manifest.
   3. Declarations with their provenance, and guard status as a protected field (DG4).
   4. A compatibility policy (decision 9) before the first release, since the release fixes the format. No manifest exists outside development yet, so the format can change freely until then.

## 4. Risks and how they are resolved

1. Magic handling. Resolved: use IPython's transform (a standard library, with a loose lower bound so installing steady-py never moves a kernel's IPython). Corpus evidence: of 1,108 cells that today's blanking can't parse, the transform fixes 570, including all 67 valid-Python cells blanking corrupts, and breaks none. The 538 that fail either way are not valid Python (largely Python 2 and broken code) and get K2's diagnostic.
2. Live mode sees only executed cells, in execution order, without cell positions. Accepted: reconciliation gives the best information available, locating lines by their text, with positions as hints where they exist.
3. Shell guards. Resolved: every shell-joined (`&&`, `||`) or shell-conditional install line counts as guarded and gets a warning, never delete advice. Corpus evidence: 88 lines in 35 notebooks; the Kaggle ones are mostly `cd dir && pip install .`, local installs that Cell 2 can't cover anyway.
4. Installed-state capture changing from `pip freeze` to `importlib.metadata`. Resolved: run both, report every disagreement, and switch only once the disagreements are understood. Each method is expected to have its own quirks.
5. Pins taken from the wrong interpreter (DG5), when the CLI runs under pipx, `uv tool` or conda base while the kernel uses another environment. Resolved for the release: snapshot warns when most of a notebook's third-party imports aren't installed in the CLI's interpreter, naming it. A `--python` option like pip's can follow the release. Live mode can't hit this.
6. Harvesting regressions on patterns no test anticipated. Resolved: a before/after corpus scan for every harvesting change.

## 5. Steps

Each step runs in its own context, ends with a handoff, and is reviewed before the next begins. Steps 1 and 2 are independent; 3 to 5 depend on them. Each step's done criterion: its rows' known-bug tests pass (the strict xfails flip to passing tests, and deferred rows get their tests in the step), the characterization tests still pass or changed with a stated reason, the unit and touched tiers pass, and harvesting changes have a clean corpus diff.

1. Install-line pipeline. The largest step; plans are revisited after it.
   1. One reader for file and live mode, with notebook-order positions: G15, D1, P7.
   2. Magics through IPython's transform, then one AST: K1, K2, G13. When a cell uses syntax newer than steady-py's interpreter (for example `lazy import` before 3.15), K2's diagnostic names both versions; steady-py doesn't rewrite such syntax.
   3. Guard detection: G1, G3, G6, and G9 (installs inside functions: 123 lines in 44 notebooks), settled here.
   4. Pip-style argument parsing: G4, G5, G11, G12, G14, G16 (with R1), G17, G18, G19, P4, C2, D5, and CH3 (`name @ url` stored as written). Needs the offline-wheel decision first (section 7).
2. Installed-state capture and the import join.
   1. Capture, run side by side first: E1, E2a, E2b, E3, E5, E6, E7, E8, K6, C1, and the DG5 warning.
   2. One `root_dir` derivation and one import-to-distribution join: ED1 to ED6, P2, G2, K12 (in live mode it drops nearly every pin on Kaggle), and development.md's known bug that live-kernel local modules outside `notebook_dir` are all tagged `root_dir`. check's missing-root-module message gains a `--root-dir` hint.
3. Manifest model: P3, P5, CH2, CH1, DG4. Removes the `scan` verb, whose delta becomes snapshot's (snapshot without a write flag already writes nothing); D4 and the scan-specific tests go with it, and README and HELP drop it.
4. Declarations and install-line reconciliation, per review_findings.md: DG7, DG2, and the `/kaggle/input` offline installs. Includes development.md's known bug that the live-session recipe is clunky, since the round trip runs there.
5. Snapshot manifest updating, per review_findings.md.

## 6. Point fixes

Not on the features' path; each is done when convenient, in any step.

1. Cell 2 runtime: R1 to R5, D2, D3, D6, DG6. R4, R5 and D2 need the Docker tier.
2. Validation: K8, LV1, LV2, CI1, CI2.
3. Resolution and output: P1, P6, LV4, K9.
4. Output safety: K7, atomic `--in-place` writes, and ownership-proven cell removal (resolves K11).

## 7. Decisions and when they are needed

Numbers refer to the matrix's "Decisions that unblock rows".

1. Before step 1.4: offline `/kaggle/input/...whl` installs (pin, keep as raw installs, or report as needing the attached dataset).
2. Before step 2.1: none open.
3. Before step 3: decision 9 (manifest compatibility) and CH2 (decision 10).
4. Before step 4: decision 8 (exit code for a heuristic finding `not_checked_at_generation`), and the install-lines spec's open decisions 10.1 to 10.4.
5. Before step 5: the manifest-updating open items (baseline, GPU record, `custom_sourced` and `local_modules` under the update rule; `generated_at` and the hash when nothing changed; update and rebuild with `--in-place`).
6. With the runtime point fixes: decision 4 (build-tag policy, DG6).
7. Any time: decision 1 (the `--universal` and `--full-freeze` scope cut), which removes or schedules G10, LV3 and E4.

## 8. After the plan, before release

1. development.md tasks 22 (review user messaging), 23 (hand-test every mode) and 25 (license).
2. A FAQ: steady-py beside Kaggle's "Pin to original environment" and beside `pip freeze`, what steady-py pins and what it doesn't (transitive dependencies), and the workflow: snapshot where the notebook runs, check from anywhere.
3. Bring development.md's Testing section up to date, or point it to working_rules.md and test_foundations_design.md.
