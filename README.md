# steady-py

**In one sentence:** steady-py looks at what your notebook actually needs to run, and adds two cells to the top of it so it runs the same way when someone else opens it — next week, next year, or on a different machine.

You don't need to be a software engineer to use it. This guide assumes you can run a notebook cell and a command in a terminal. Terms you may not know are explained the first time they appear, and there's a short glossary at the end.

If you received a notebook that someone else set up with steady-py, and something in its setup cell went wrong, see [HELP.md](https://github.com/flyinacres/steady-py/blob/main/HELP.md) instead.

---

## What it does

Notebooks break over time because the packages they use keep changing. steady-py records the exact versions your notebook works with today, and gives the notebook two cells that install exactly those versions for whoever runs it next.

It has two commands:

- **`snapshot`** — looks at a notebook, reports what it needs, and produces the two setup cells. It changes nothing unless you ask it to write them into the notebook for you.
- **`check`** — looks at a notebook that already has setup cells and asks PyPI (the public index where Python packages live) whether any of its recorded versions have become a problem since.

---

## Install

On your own computer:

```bash
pip install steady-py
```

In a notebook on Kaggle, Colab or Jupyter, run this in a cell:

```
%pip install steady-py
```

steady-py needs Python 3.11 or newer and an internet connection.

---

## Quick start: a notebook file on your computer

1. **Finish your notebook, then run it from the top in a fresh kernel.** (The *kernel* is the Python process behind your notebook; restarting it clears anything left over from earlier experiments.) This makes sure the notebook really works with the packages you have now.
2. **In a terminal, in the notebook's folder, run:**

   ```bash
   steady-py snapshot my_notebook.ipynb --output
   ```

   This writes `my_notebook_merged.ipynb`: your notebook with the two setup cells added at the top. Your original file is not touched.
3. **Share the `_merged` notebook.** When someone runs it from the top, the setup cells run first and install the versions you used.

Prefer to paste the cells yourself? Leave out `--output`. steady-py then prints both cells: paste the first into a new **Markdown** cell and the second into a new **Code** cell, both at the very top of the notebook.

If the `steady-py` command isn't found, `python -m steady_py` works the same way (`python -m steady_py snapshot my_notebook.ipynb --output`).

**Use the same Python environment the notebook runs in.** steady-py records the versions installed where *it* runs. If you have several environments (for example one per project), run it from the one your notebook uses.

---

## Quick start: inside a running notebook (Kaggle, Colab, Jupyter)

On Kaggle and Colab there is no terminal to run a file through, so run steady-py from inside the notebook instead:

1. Run your whole notebook from the top in a fresh session.
2. Add a cell at the end and run it:

   ```python
   %pip install steady-py
   import steady_py; steady_py.show_snapshot()
   ```

3. Read what it reports. Its last line says what to do next. If warnings need attention, fix or accept them, then restart the session, run all, and run the cell again: snapshot reads every cell that ran in the session, so a fix only shows after a restart.
4. Copy the block under **STEP 1** into a new **Markdown** cell and the block under **STEP 2** into a new **Code** cell, both at the very top of the notebook.
5. Delete the cell you added in step 2 and save.

If you forget step 5, the cell runs for everyone who runs your notebook. It changes nothing in the notebook, its output starts by telling readers it is meant for the author, and in a Kaggle "Save Version" run it does nothing at all.

**A trap to know about:** in a running notebook, steady-py looks at *everything that has run in this session*, including cells you ran and later deleted. If you tried a package and abandoned it, it can still end up in the setup cell. Restarting and running the notebook from the top first avoids this. Running on a saved file doesn't have this problem, because it only reads what's in the file.

---

## Your own install lines, and telling steady-py what you know

steady-py adds Cell 2 at the top and leaves your own cells as they are, so your own install lines (`!pip install ...`, `%pip install ...`, `conda install ...`) still run after Cell 2. snapshot lists each one under **INSTALL LINES**, in notebook order, with what to do about it:

- **Conflicting** (⚠️, exit code 1): the line can undo Cell 2, for example `-U` upgrades a pinned package or a different `==` replaces it. Delete it, or remove the package from it. If the notebook doesn't import that package, replace the line with an `install` entry (below) so the version is still recorded.
- **Redundant** (ℹ️): the line repeats what Cell 2 installs and changes nothing. No change needed; keeping it is harmless.
- **Above Cell 2** (ℹ️): the line runs before Cell 2's pins, so an unpinned line brings in the latest release and its dependencies, and Cell 2 restores the pins but not those dependencies. Move Cells 1 and 2 to the top; snapshot `--in-place` or `--output` puts them there.
- **Lines Cell 2 can't reproduce**: a line inside an `if`, `try` or function, a package name held in a variable, a `conda` line, a `-r requirements.txt` file, or a package that isn't installed where you ran steady-py. These lines stay, and snapshot proposes an entry for each.

steady-py never edits your code; you make each change yourself.

### Declarations

Some things steady-py can't work out from your code. You tell it in a cell of its own, anywhere in the notebook:

```python
STEADY_PY_DECLARATIONS = [
    {"install": "acme-utils", "version": "2.1.0", "reason": "our private helper"},
    {"ignore": "torch", "group": "gpu", "reason": "installed by the platform check below"},
    {"accept": "numpy", "reason": "the -U line is deliberate"},
]
```

This cell is read, never run for its effect, so it does nothing when the notebook runs. Every entry needs a `reason`. There are three kinds:

| Kind | Meaning | Other keys it takes |
| --- | --- | --- |
| `install` | Cell 2 installs this package. | `version` (exact, no `==`), `extras` (a list), `url` (a direct source such as a git URL, instead of `version`), `index_url`, `extra_index_url`, `find_links`. Without `version` or `url`, the installed version is used, else PyPI's latest. |
| `ignore` | Your own cells install this package; Cell 2 leaves it alone. | `version` (what you made the notebook with, shown by Cell 2), `group` (a name tying alternatives together, such as one package for Colab and another elsewhere). |
| `accept` | A finding about this package or module is fine for your notebook; it stays in the report, marked accepted, and stops counting toward exit code 1. | none |

snapshot proposes entries ready to paste: copy them into the cell, replace the `...` with your reason, and run snapshot again. A proposal with `<package>` in it needs a real package name first. When an entry changes an outcome, snapshot and check list it under **Declarations applied**; an `accept` that no longer matches anything is listed there too, so you can remove it.

An entry steady-py can't read (a missing reason, an unknown key, a misspelled kind) stops snapshot with exit code 2 and says which entry is wrong, since going on without it would pin packages you asked it to leave alone.

From the command line, `--declarations FILE` reads the same `STEADY_PY_DECLARATIONS = [...]` from a file and applies it to every notebook, which suits a folder of course notebooks. A notebook's own cell wins where the two disagree, with a warning.

---

## What the two cells do

**Cell 1 (Markdown)** tells readers what's about to happen: that Cell 2 checks the Python version and installs the recorded package versions, that it needs internet access, and, when relevant, which GPU the notebook was made on and which packages use special hardware-specific builds.

**Cell 2 (Code)** does the work:

1. It holds the **manifest**: the list of packages and exact versions, the Python version, GPU details, when it was made, and a fingerprint (a *hash*) of all of that. The fingerprint lets `check` notice if anyone edits the list by hand, so don't edit it — regenerate instead.
2. It installs steady-py itself, at the exact version that made the cell.
3. It installs each recorded package, one at a time, so one package that fails never stops the others. Packages that are already installed at the right version are skipped.
4. It prints a summary: everything installed, or which packages failed and what to try. Packages you declared `ignore` are listed separately, with what is installed, and aren't counted. If you kept a conflicting install line, the summary names the package a later cell can change.

Cell 2 never stops the notebook. If the Python version differs from yours, or a package won't install, it says so and carries on. The messages people can see while it runs are explained in [HELP.md](https://github.com/flyinacres/steady-py/blob/main/HELP.md).

Cell 2 may also contain comment lines (starting with `#`) listing things it noticed but won't install. Those are explained under [Messages you might see](#messages-you-might-see) below.

---

## Commands

Every command takes a notebook file or a folder. Options common to both: `--quiet` (show errors only), `--verbose` (show extra detail) and `--format json` (machine-readable output, for scripts and other tools; see [Using it from Python](#using-it-from-python-or-other-tools)).

### snapshot

```bash
steady-py snapshot my_notebook.ipynb
steady-py snapshot my_notebook.ipynb --output
steady-py snapshot course_materials/ --output-dir locked/
```

Reports each notebook's packages, warnings and notices, and the GPU situation, produces the two setup cells, and checks the recorded versions against PyPI right away (see [What check reports](#what-check-reports)). With none of the write options it writes nothing and just prints the cells; for a folder you get a readable summary.

| Option | What it does |
| --- | --- |
| `--output` | Write a copy of each notebook with the setup cells added, next to the original, named with a suffix (default `_merged`). |
| `--output-dir DIR` | Write the copies into `DIR` instead, mirroring the folder structure so same-named notebooks in different folders don't collide. No suffix unless you give one. |
| `--in-place` | Replace the setup cells in the original notebook itself. |
| `--suffix SUFFIX` | The name suffix for `--output` / `--output-dir` copies. For a folder, files already ending in it are skipped, so steady-py doesn't read its own copies (default `_merged`). |
| `--universal [FILENAME]` | Folders only: also write one combined requirements file for every notebook (default name `requirements-all.txt`). |
| `--timeout SECONDS` | How long Cell 2 lets each package install run before giving up (default 120). |
| `--full-freeze` | Also store the complete list of everything installed in your environment in Cell 2, as a record. It isn't installed automatically. |
| `--declarations FILE` | Read declarations from a file as well as from each notebook's own cell (see [Declarations](#declarations)). |

Running snapshot on a notebook that already has setup cells shows what would change since the old ones, field by field, and with `--in-place` replaces them. If nothing changed, the notebook is left exactly as it was. The setup cell records when each package's entry last changed, and the date at the top of Cell 2 is when anything in it last changed, not when snapshot last ran. To fix a flagged package, change your own environment first (install the version you want), then run snapshot again with `--in-place`.

If the setup cells were written by a newer steady-py, snapshot won't replace them; upgrade steady-py first. Setup cells from an older format are replaced, with a warning.

### check

```bash
steady-py check my_notebook_merged.ipynb
steady-py check course_materials/ --format json
```

Reads the manifest stored in each notebook and compares it with what PyPI says today. Read-only. A notebook without setup cells simply has nothing to check.

| Option | What it does |
| --- | --- |
| `--root-dir DIR` | The project root to look in for your own helper files that were found through a project root when the notebook was set up. Without it, steady-py uses the nearest folder above the notebook holding `.git`, `pyproject.toml`, `setup.py` or `setup.cfg`, as it did at setup; failing that, the folder you gave it, or for a single notebook those files are reported as "could not check". |

### Exit codes

Useful when running steady-py from a script or a scheduled job:

- **0** — everything was processed and nothing needs attention.
- **1** — the job was done but something needs attention: `check` found a problem, some notebooks in a folder were skipped, a notebook imports something only your machine has (your own package in editable mode, a package installed from a local file, a module on a custom path) or that more than one installed package provides, or one of its install lines can undo Cell 2. An `accept` declaration stops a finding counting here (see [Declarations](#declarations)).
- **2** — the job couldn't be done: a bad option, a missing path, a notebook that couldn't be read, or `check` couldn't verify something.

For `check`, a "worth reviewing" item that was already present when the notebook was set up doesn't count as a problem.

---

## Working with a folder of notebooks (instructors)

Point any command at a folder instead of a file. steady-py finds every notebook in it, however deeply nested, and skips hidden folders, virtual environments, non-Python notebooks and its own `_merged` copies.

```bash
steady-py snapshot course_materials/             # a summary across every notebook
steady-py snapshot course_materials/ --output    # add setup cells to a copy of each
steady-py check course_materials/                # re-check them all later
```

The folder summary lists which packages are installed, which couldn't be found (and in which notebooks), optional imports, warnings, system and conda commands, and the GPU and download-index situation.

When snapshot writes files, it finishes with a **batch dependency validation** section: each problem listed once, with the notebooks it affects, so forty notebooks using the same withdrawn release show up as one item.

If some notebooks can't be read, the rest are still written, the skipped ones are listed with the reason, and the exit code is 1. A `--universal` file notes any skipped notebooks at the top.

**Worth knowing:**

- `--output-dir` copies notebooks only, not the data files next to them. A notebook that reads `data/data.csv` won't find it from the new location unless you copy the data too.
- A notebook of your own whose name happens to end in `_merged` is skipped.
- Recognizing your own helper files (so they aren't mistaken for missing packages) looks next to each notebook and in the folder you pointed at, not in every folder in between.

---

## Checking a notebook later

```bash
steady-py check my_notebook_merged.ipynb
```

Run this any time, for example before a course starts. It reads the recorded versions and asks PyPI whether any of them have become a problem. It can't tell whether your code still works with newer versions, and it doesn't look for security issues.

### What check reports

Findings come in groups:

- **🔴 Confirmed issues** — something is definitely wrong and installing will likely fail or misbehave.
- **🟡 Worth reviewing** — a heuristic, not proof of a problem.
- **⚠️ Could not check** — steady-py couldn't reach PyPI or read something, so it can't say.
- **ℹ️ Custom sources** — packages that aren't on PyPI and weren't when the notebook was set up (a private or local package). Expected; not counted as a problem.

Each finding is marked **[new]** or **[known]** (already present when the notebook was set up), so you can see what changed since. When the setup cells are made, snapshot runs the same checks and records what it found; that's what "known" compares against.

| Finding | Group | What it means | What to do |
| --- | --- | --- | --- |
| `yanked` | Confirmed | The recorded release was withdrawn from PyPI by its author, usually because of a serious bug. | Pick another version in your environment and snapshot again. |
| `removed` | Confirmed | That release no longer exists on PyPI, though the package does. | Same as above. |
| `not_found_on_pypi` | Confirmed | The package isn't on PyPI at all. It may be private or local (then no action is needed), or the name may be misspelled. | If it's private, make sure recipients can get it. |
| `conflict` | Confirmed | Two recorded packages need incompatible versions of something, directly or several levels down. | Adjust versions in your environment and snapshot again. |
| `unsupported_python` | Confirmed | The recorded release says it doesn't support the notebook's Python version. | Choose a release that does. |
| `tampered` | Confirmed | The manifest was changed by hand after it was made. | Regenerate with snapshot. |
| `unread_fields` | Confirmed | A newer steady-py wrote the manifest with fields this one can't check, so a clean result would claim too much. | Upgrade steady-py. |
| `local_module_missing` | Confirmed | One of your own helper files, recorded next to the notebook, is gone. | Restore it, or snapshot again if the project changed. |
| `stale` | Worth reviewing | The package has had no release in about two years. | Consider whether it's still maintained. |
| `major_bump` | Worth reviewing | A newer major version exists. Not a problem by itself. | Nothing unless you want to upgrade. |
| `unverifiable_custom_index` | Worth reviewing | The version carries a hardware tag (like `+cu121`), so it came from a special download index PyPI can't check. | Make sure recipients can reach that index. |

---

## Messages you might see

### While snapshot runs

| Message | What it means |
| --- | --- |
| **⚡ Active accelerator detected: [device]** | A GPU library was imported, and a GPU was confirmed working where you ran steady-py. |
| **⚠️ Acceleration Framework (...) imported, but NO active accelerator detected in host runtime.** | A GPU library (such as `torch`) is imported, but no GPU was available. If the notebook needs a GPU, set it up on one. |
| **💡 Extra Dependency Promotion: importing 'x.y' automatically promoted requirement to 'package[y]==...'** | Some packages have optional add-ons (*extras*) that only install when asked for. The notebook uses one, so steady-py records it. |
| **⚠️ Dynamic import detected via variable '...'** | The notebook loads a package by a name held in a variable, which can't be worked out by reading the code. Make sure that package is installed. |
| **⚠️ Conflicting Explicit Pins for '...'** / **Conflicting Scoped Flags for '...'** | The notebook installs the same package twice with different versions or options; the later one is used. |
| **⚠️ Cell N references an external requirements file** | The notebook installs from a file such as `requirements.txt`. Share that file with the notebook. |
| **'...' is installed from a non-standard source (git/URL/local file), not PyPI** | Installed exactly as written, but steady-py can't check it, and recipients must be able to reach it. |
| **ℹ️ Cell N uses a system install command** / **uses 'conda install'** | Commands like `apt-get` or `conda install` are outside Python's package system; readers must run them themselves. |
| **Specific hardware build detected: ... with no download URL harvested** | A package version made for specific hardware (like `torch==2.3.1+cu121`) has no download index in the notebook; recipients may not be able to install it. |
| **Packages neither installed nor found as local files** | Imported, but not installed and not found as a file next to the notebook. It may be a real gap, or your own code found some other way (an IDE project root, `PYTHONPATH`, Databricks Repos). Run the notebook to confirm. |

### Comment lines in Cell 2

Lines starting with `#` in Cell 2 are notes; nothing on them is installed.

| Line | What it means |
| --- | --- |
| `# pkg (optional or conditional dependency inside try/except block)` | Only imported inside a fallback block (code that tries something and has a plan B), so it's treated as optional. |
| `# x (imported as 'x'; not installed here or found as a local file -- verify ...)` | Imported, but not installed where you ran steady-py. |
| `# x (local folder/file next to notebook; ensure sibling files were shared)` | Your own code, next to the notebook. Share it with the notebook. |
| `# x (provided automatically by platform like Colab/Databricks; no install needed)` | Something the platform provides; nothing to install. |
| `# x (core Python build/packaging tool; excluded from requirement lockfiles)` | Tools like `pip` or `setuptools`; deliberately left out. |
| `# x (... found on a system-dependent path, which can't and shouldn't be shared directly ...)` | Installed on your computer from a local file. That location only exists on your machine, so it isn't recorded, and a warning says so. |
| `# x (imported as 'm'; your in-development package, installed in editable mode; ...)` | Your own package, installed in editable mode. Runners can't install it; a warning suggests publishing it or sharing it from Git, naming the Git source when the folder is a checkout. Its own requirements are pinned. |
| `# m (importable on this machine from a folder no installed package owns; ...)` | Found through `PYTHONPATH`, a `.pth` file or `conda develop`, not an installed package, so it can't be pinned. |
| `# m (provided by more than one installed package: ...)` | Several installed packages ship this module, so none is pinned; a warning names them. |
| `# x (... installed from a direct URL, not PyPI ...)` | Installed from a web address, such as a git repository. The address is recorded and installed as written. |
| `# tool (installed via cell command; ...)` | Installed by an install command in the notebook but never imported, like a command-line tool. |
| `# pkg (imported inside script generated via %%writefile ...)` | Used by a script the notebook writes to disk. |

### In Cell 1

| Line | What it means |
| --- | --- |
| **Hardware Acceleration** | The notebook was made on a machine with a working GPU; readers should enable one. |
| **Specific Package Builds Detected** | Some packages use hardware-specific builds, listed with the download index where one was found. |
| **Network Notice** | Installing needs internet access. |

---

## Using it from Python, or other tools

Everything the commands do is available as functions. They return results and never print or exit, so they work in scripts and inside a running notebook:

```python
import steady_py

needs = steady_py.snapshot("my_notebook.ipynb")
snap = steady_py.snapshot("course_materials", steady_py.SnapshotOptions(write_mode="companion"))
checked = steady_py.check("my_notebook_merged.ipynb")
```

Called with no target, `snapshot()` analyzes the running notebook session; `show_snapshot()` does the same and prints its report and both cells. Options are small classes (`SnapshotOptions`, `CheckOptions`); results (`SnapshotResult`, `CheckResult`) hold one entry per notebook plus an overall section. `steady_py.install(manifest)` is what Cell 2 calls; it returns what installed and what failed.

For other tools, `--format json` prints the same information as JSON, with a `schema_version` field that changes if the structure does. The structures are defined in `src/steady_py/results.py`.

---

## Things it can't do yet

- It pins the packages your notebook imports directly, not the packages those depend on, so those can still change between installs. `check` does look through them for conflicts and withdrawn releases.
- A package loaded by a name held in a variable can't be identified; it's flagged instead.
- Some libraries load an optional part by name (`holoviews.extension("bokeh")` quietly needs `bokeh`). That never appears as an import, so it isn't detected.
- If the notebook adds a folder to its search path while running (`sys.path.append(...)`), files imported from there may be reported as missing packages. Relative imports (`from . import x`) aren't detected.
- Packages installed from a git address or web link are installed as written, but can't be checked later, and their own dependencies aren't examined.
- It confirms a GPU was *available*, not that the notebook used it. It flags hardware-specific builds (like `+cu121`) but can't fix a mismatch.
- One set of setup cells describes one environment. A notebook meant to run both on an NVIDIA GPU and on a Mac records whichever machine you ran it on.
- Running on a saved file and inside a live session can give different answers (see the trap above).

For design details and ongoing work, see [development.md](https://github.com/flyinacres/steady-py/blob/main/development.md).

---

## A few terms explained

- **Package / dependency:** code someone else wrote that your notebook uses, installed with `pip install`.
- **PyPI:** the public index where Python packages are published, and where `pip` downloads them from.
- **Pinning:** recording one exact version (`numpy==1.26.4`) instead of "whatever is newest", so results stay the same over time.
- **Manifest:** the list of pinned packages steady-py stores in Cell 2.
- **Kernel / session:** the running Python process behind a notebook. Restarting it clears everything in memory.
- **Environment:** your Python version plus the packages installed in it.
- **Guarded / optional import:** an import inside `try: ... except: ...`, with a plan B if it fails. steady-py treats it as optional.
- **Extras:** optional add-ons for a package, installed as `package[extra]`.
- **GPU / accelerator:** hardware that speeds up heavy numerical work such as machine learning. Includes NVIDIA GPUs, Apple Silicon (MPS) and Google TPUs.
- **Exit code:** a number a command returns when it finishes, which scripts use to tell success from failure.
