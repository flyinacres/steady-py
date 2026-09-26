# Notebook setup help

You're running a notebook that someone set up with steady-py, and one of the two cells at its top printed something you don't understand. This page explains each message and what to do about it.

You don't need to know how steady-py works. Find the message below (Ctrl+F or Cmd+F with a few words from it works well), and read what to do.

**The short version:** Cell 2 installs the exact package versions the notebook's author used, one at a time. It never stops the notebook. If something fails, it says which package and why, then carries on with the rest. The end of its output tells you whether everything installed.

---

## Messages from Cell 2 (the code cell)

### Before it starts

**"⚠️ Could not install the pinned steady-py==X helper (it may not be released yet)."**
followed by **"Proceeding with whatever steady_py is already available, if any..."**

Cell 2 first installs steady-py itself, at the exact version that made this notebook, and that didn't work. The text printed below the message is pip's own explanation. The usual causes are no internet access, or a version that isn't published.

_What to do:_ check your internet connection and run Cell 2 again. If it then fails with **"No module named 'steady_py'"**, steady-py couldn't be installed at all; run `%pip install steady-py` in a cell and try Cell 2 again. If the problem continues, tell whoever shared the notebook.

**"⚠️ This code was created with Python 3.X. You are trying to run it with 3.Y."**
**"If installation fails, consider changing your runtime Python version back to 3.X."**

The notebook was made with a different Python version than the one you're running. Cell 2 carries on anyway. Often everything still works.

_What to do:_ usually nothing. If a package fails to install further down, this is the most likely reason: switch your environment to the Python version named in the message (on Kaggle and Colab this is part of the runtime or environment settings), then run Cell 2 again.

**"Applying verified environment dependencies [date]..."** and **"💡 Note: Dependencies are installed sequentially to prevent index conflicts."**

Informational. The date is when the author set the notebook up. Packages are installed one at a time, so one failure never blocks the others.

### For each package

Each package gets a numbered line such as `[3/12]`.

**"⚡ package (version) already satisfied in environment"**

That exact version is already installed, so nothing needed doing. Good.

**"📦 Installing package==version..."** then **"✅ package==version installed successfully"**

Installed. Good.

**"❌ package==version failed to install (exit code N)"**

This package didn't install. The lines below it say more:

- **"Author Verified Version"** — the version the author used.
- **"Scoped Flags"** — extra options this package is installed with, such as a special download address.
- **"Error"** — the end of pip's own error message. This is where the real reason is.

Common reasons, and what to do:

- _No internet, or a network hiccup:_ check your connection and run Cell 2 again.
- _Wrong Python version:_ the message may mention "Requires-Python", or say no matching version was found. See the Python-version message above.
- _A version built for specific hardware_ (its version ends in something like `+cu121`, meaning it's made for a particular NVIDIA graphics card setup): it may not exist for your machine. Make sure your runtime has a GPU enabled if the notebook expects one.
- _The version no longer exists or was withdrawn:_ tell whoever shared the notebook; they need to update it.

**"⚠️ This package is custom-specified by the notebook's author (not on public PyPI)."**

This package isn't on PyPI (the public index packages are normally installed from). It comes from somewhere the author set up, such as a private package server.

_What to do:_ ask the author where the package is available now.

**"❌ Installation timed out after Ns."**

The install took longer than the time allowed and was stopped. Usually a slow connection or a very large package.

_What to do:_ run Cell 2 again. Packages that already installed are skipped, so it's quicker the second time.

**"❌ Execution failed: ..."**

pip itself couldn't be started. This is rare, and usually means something is wrong with the Python environment itself.

_What to do:_ restart the kernel (the Python process behind the notebook, usually under a "Kernel" or "Runtime" menu) and run Cell 2 again. If it keeps happening, the text after "Execution failed" is the thing to search for or pass on.

**"⚠️ Dependency Drift: Installing 'package' caused 'other' to drift from A ➔ B"**

Installing one package changed the version of another package that was installed earlier. The notebook may still work, but it's no longer running exactly what the author used.

_What to do:_ nothing right away. If the notebook later fails with an error involving the package that moved, tell whoever shared the notebook; two of its packages don't agree on a version.

### Packages from other sources

**"📎 Installing non-standard sources (git/URL/local file)..."**

Some packages come from somewhere other than PyPI: a git repository, a web link, or a file on the author's computer. They are installed exactly as the author wrote them, and steady-py can't check them.

**"❌ ... failed to install"** followed by **"This is a custom-specified source (git/URL/local file), not a standard PyPI package."**

That source couldn't be reached. It may be private, moved, or only exist on the author's computer.

_What to do:_ ask whoever shared the notebook where it's available now. The rest of the notebook will fail at the first line that needs it.

### The summary at the end

**"✅ Setup complete! All N/N dependencies verified."**

Everything installed. Carry on with the rest of the notebook.

**"⚠️ Setup completed with issues: X/N packages installed."**

Some packages didn't install; the ❌ lines above say which and why. The notebook may still run if it doesn't need those packages right away.

The troubleshooting steps printed below it, expanded:

1. **Internet access.** On Kaggle, internet has to be switched on in the notebook's settings. On a work or school network, a firewall may block package downloads.
2. **Trying without the exact version.** `!pip install package` (without `==version`) installs the newest version instead of the author's. That often gets you going, but the notebook may then behave differently than it did for the author. Treat it as a last resort, and try the exact version (`!pip install package==version`, copied from the ❌ line) first.
3. **This page.**

**"⚠️ Note: You may need to restart the kernel to use updated packages."**

Cell 2 installed or changed something. If you've already run other cells in this session, Python may still be using the old versions it loaded then.

_What to do:_ restart the kernel, then run the notebook from the top. Cell 2 runs again, finds everything already installed, and finishes quickly.

---

## Things Cell 1 (the description) may mention

Cell 1 only explains; it doesn't install anything.

**"Hardware Acceleration: This notebook was created using a GPU accelerator..."**

The author ran this on a machine with a graphics card (GPU), which speeds up heavy computation such as machine learning.

_What to do:_ turn on a GPU if your platform offers one (Kaggle: notebook settings, Accelerator; Colab: Runtime, Change runtime type). Without one the notebook may still run, only more slowly, or it may fail if it truly needs a GPU.

**"Specific Package Builds Detected"**

Some packages are versions built for specific hardware, listed with the address they're downloaded from. If one fails to install, see the hardware-specific reason under "failed to install" above.

**"Network Notice"**

Installing packages needs internet access.

---

## Lines in Cell 2 starting with `#`

Anything starting with `#` is a note, not an instruction; nothing on those lines is installed. You don't have to do anything about them, but if something fails later they can explain why.

| What it looks like | What it means |
| --- | --- |
| `# pkg (optional or conditional dependency inside try/except block)` | The notebook only uses this as a backup option, so it isn't required. |
| `# x (imported as 'x'; not found via pip-freeze or local file scan ...)` | The notebook uses this, but it wasn't installed when the author set it up. If the notebook later fails with "No module named 'x'", ask the author about it. |
| `# x (local folder/file next to notebook; ensure sibling files were shared)` | This is a file that should come with the notebook. If it's missing, ask for the notebook's other files too, not just the notebook. |
| `# x (provided automatically by platform like Colab/Databricks; no install needed)` | Provided by the platform itself. Nothing to do. |
| `# x (core Python build/packaging tool; excluded from requirement lockfiles)` | Tools such as `pip` itself. Deliberately left out. |
| `# x (... found on a system-dependent path, which can't and shouldn't be shared directly ...)` | Installed on the author's computer from a location only they have. It won't be installed for you; ask the author where to get it. |
| `# x (... installed from a direct URL, not PyPI ...)` | Comes from a web address, such as a git repository. Cell 2 installs it from there, so you need to be able to reach that address. |
| `# tool (installed via cell command; ...)` | A tool the notebook installs with a command rather than using in Python code. |
| `# pkg (imported inside script generated via %%writefile ...)` | Used by a script the notebook writes out and runs. |

---

## Still stuck?

1. Read the full error text, not just the summary line. The real reason is almost always in pip's message just above it.
2. Restart the kernel and run the notebook from the top. This fixes more problems than you'd expect.
3. Ask whoever gave you the notebook. They know what it needs, and they can re-run steady-py to update it.

If you set notebooks up with steady-py yourself, the [README](https://github.com/flyinacres/steady-py/blob/main/README.md) covers the author's side.
