# Working rules

How work on steady-py is done. Product rules (what the tool does for users) live in review_findings.md, Principles, and development.md. Test support design lives in test_foundations_design.md. The plan this work follows is fix_plan.md.

## 1. Code standards

1. Each decision has one owner. Entry points (file, directory, live session, check) differ only in how they gather input and render output, never in how they classify, resolve or judge.
2. When a change touches a decision, first find every place in the code that makes it, and consolidate or update all of them in the same change. A partial edit is not complete.
3. Diagnose before fixing: name a cause consistent with every symptom. If the symptoms don't fit one cause, say so instead of forcing one.
4. Results and records are typed dataclasses, not tuples or loose dicts. Code touched by a change passes `mypy --strict` (H1).
5. No broad `except Exception`, except around third-party probes on an explicit allowlist (H2) and at the per-notebook boundary of a directory run, where one notebook's failure is logged with its path and reported, and the run continues. An error a user can act on becomes a diagnostic; one that stops the job maps to the exit rule: 0 clean, 1 needs attention, 2 could not do the job.
6. Diagnostics carry the notebook and the cell's position in the notebook as the user sees it.
7. The library never configures logging; only the CLI entry point does (H3).
8. Use standard libraries (packaging, resolvelib, IPython, importlib.metadata) rather than parallel implementations.
9. Comments and display text are never a source of truth. Anything needed later is a structured, hashed manifest field; anything needed within a run is a typed field, never parsed back out of rendered text.

## 2. Evidence

1. Verify claims against the code and by running it. Don't describe a mechanism that hasn't been checked.
2. Probe current behavior before planning a change or a test.
3. Changes to install-line harvesting are checked with a before/after run of `corpus_install_scan.py` over the corpus, since unit tests cover only the cases someone thought of. Ron runs corpus scans; ask for a run directly.
4. A change to a source of truth (for example, installed-state capture) runs the old and new methods side by side and reports every disagreement before the new one replaces the old.
5. When an approach stops paying off, say so and propose another path instead of grinding. Flag effort-to-impact problems and let Ron decide.

## 3. Tests

1. Assert at stable boundaries: exit code, the `--format json` report, the manifest literal in a written notebook, Cell 2's printed output.
2. Tests never import `steady_py.<module>`; only `tests/support/` touches internals, and nothing monkeypatches steady-py. The older top-level `tests/test_*.py` files predate this rule. The hygiene selftest enforces it for `tests/behavior` and `tests/characterization`.
3. Fake only external boundaries: the network (fake PyPI) and hardware. The environment, filesystem and kernel are real.
4. Tests for a decision cover every entry point that reaches it (file, directory, live where it applies).
5. Known bugs are strict xfails through `known_bug(id, why)`, with `raises=AssertionError`. Check each with `--runxfail` to confirm it fails at the intended assertion. Add a passing control case where a fix could overshoot. A scenario that didn't set up raises `RuntimeError`, never an assertion.
6. Sabotage every new test: make it fail on purpose to prove it can.
7. Before writing a group of tests, present the planned assertions as a short numbered list.
8. Tests are code: one owner per concern, no duplicated helpers, concise.
9. Old test files (`tests/test_*.py`) are deleted only once the boundary tests cover their gaps (test_foundations_design.md, §14). A step that touches a gap area writes the boundary tests for it.
10. Tiers: the unit suite runs by default; `-m venv`, `-m kernel` and `-m docker` run the others. Run the unit suite before every delivery, plus `-m venv` or `-m kernel` when that tier's code or support changes. Ron runs the Docker tier on Windows and on an Apple Silicon Mac, so everything built for it is py3-none-any.

## 4. Process

1. Work proceeds in the steps of fix_plan.md. Each step runs in its own clean context and ends with a handoff; Ron reviews before the next step starts.
2. Describe a change before producing files, and wait for agreement, except in work Ron has handed over to run unattended.
3. Small production changes for testability are allowed; list each in the delivery notes.
4. Where code proves a design document wrong, revise the document and say what changed.
5. Every delivery opens with a numbered list of full repository paths, each marked new or replaced, and calls out file names that occur more than once in the repository (conftest.py, pyproject.toml, __init__.py). Deliver only changed files, and end with a one-line commit message.
6. Ask Ron directly when a run or information is needed: corpus scans, Docker, tests on Windows, git.

## 5. Documents

1. Keep documents lean: remove resolved and historical content, and don't leave stale claims.
2. No time-relative language ("new", "currently", "this session") in documents meant to last.
3. Plain language, numbered lists rather than bullets so items can be referred to, no em dashes.
4. Each fact has one owner document; others refer to it by ID or section. test_triage_matrix.md owns each finding's ID, title, settled state, layer, status and step, and the decisions that unblock rows. review_findings.md owns what is wrong and why, each finding's fix, the corpus evidence, rejected items and, until step 4, the feature specs. fix_plan.md owns how and when: scope, contracts, risks and each step's approach. A new finding is one matrix row, plus a review_findings.md entry if it needs explanation.

## 6. Environment

1. Ron develops on Windows 11 with Python 3.13; the sandbox is Linux. Windows items still to verify are in test_foundations_design.md, §13.
2. Setup after cloning: `pip install -e . pytest nbformat ipykernel jupyter_client`, adding `--break-system-packages` where the Python is externally managed (PEP 668), as in the sandbox. The venv tier downloads its wheelhouse on first use.
3. The sandbox can't run Docker.
