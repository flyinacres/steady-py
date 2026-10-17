"""Installed-state capture, old against new (working_rules.md §2.4): pip freeze plus the
direct_url.json overlay (`get_installed_environment`) against importlib.metadata
(`read_installed`). Runs as a script in the interpreter under test, which needs only steady-py
installed, and prints one JSON object: each disagreement by canonical name."""
import json
import sys

from steady_py import installed, util


def disagreements() -> dict:
    old, _ = installed.get_installed_environment()
    old = {k: v for k, v in old.items() if k == util.canonicalize_pkg_name(k)}  # drop the lowercase aliases
    new = installed.read_installed()
    return {
        "only_old": {k: old[k] for k in sorted(old.keys() - new.keys())},
        "only_new": {k: new[k] for k in sorted(new.keys() - old.keys())},
        "differ": {k: {"old": old[k], "new": new[k]} for k in sorted(old.keys() & new.keys()) if old[k] != new[k]},
        "agree": len([k for k in old.keys() & new.keys() if old[k] == new[k]]),
        "python": sys.version.split()[0],
    }


if __name__ == "__main__":
    print(json.dumps(disagreements(), indent=1, sort_keys=True))
