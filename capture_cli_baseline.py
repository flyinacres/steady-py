#!/usr/bin/env python3
"""Captures steady-py CLI output for a fixed set of cases, to diff against after a CLI change
(e.g. task 12, the flags-to-subcommands switch).

Usage (from the repo root, with the package importable):
    python capture_cli_baseline.py > cli_baseline_before.txt
    ... make the change ...
    python capture_cli_baseline.py > cli_baseline_after.txt
    diff cli_baseline_before.txt cli_baseline_after.txt

Everything is written to a scratch directory under the system temp dir and cleaned up after each
case. Output is normalized (timestamps, cell ids, manifest hashes, absolute paths) so that an
unrelated run-to-run difference doesn't show up as a false regression. Any real difference in
stdout, stderr, exit code, or the written notebook/manifest files will appear in the diff.

This captures CLI *behavior*, not source code -- it must be run before and after the change using
the actual installed/importable steady_py package at each point.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Cell output and error messages contain emoji; on Windows, stdout attached to a redirected file
# defaults to the cp1252 encoding and raises on them. Force UTF-8 so `> file.txt` always works.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent
SRC = REPO_ROOT / "src"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "unit"

NORM = [
    (re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d"), "<TS>"),
    (re.compile(r"spy-[0-9a-f]{6}"), "<CELLID>"),
    (re.compile(r"\b[0-9a-f]{64}\b"), "<HASH>"),
    (re.compile(r"\d+\.\d+(?:\.\d+)? (?:seconds|s)\b"), "<DUR>"),
]


def norm(s: str, case_dir: Path) -> str:
    s = s.replace(str(case_dir), "<DIR>")
    for rx, rep in NORM:
        s = rx.sub(rep, s)
    return s


def nb(cells, kernel="python3", lang="python"):
    return json.dumps({
        "cells": [{"cell_type": "code", "source": [c], "metadata": {}, "outputs": [], "execution_count": None} for c in cells],
        "metadata": {"kernelspec": {"name": kernel, "language": lang, "display_name": kernel}},
        "nbformat": 4, "nbformat_minor": 5,
    })


def setup_single(case_dir: Path) -> None:
    shutil.copy(FIXTURES / "kitchen_sink.ipynb", case_dir / "kitchen_sink.ipynb")
    shutil.copy(FIXTURES / "magic_sink.ipynb", case_dir / "magic_sink.ipynb")
    (case_dir / "corrupt.ipynb").write_text("{ this is not json", encoding="utf-8")
    (case_dir / "rnb.ipynb").write_text(nb(["library(ggplot2)"], "ir", "R"), encoding="utf-8")
    (case_dir / "tagged.ipynb").write_text(
        nb(["!pip install torch==2.1.0+cu118 --extra-index-url https://download.pytorch.org/whl/cu118\nimport requests"]),
        encoding="utf-8",
    )
    (case_dir / "localmod.py").write_text("x = 1\n", encoding="utf-8")
    (case_dir / "withlocal.ipynb").write_text(nb(["import localmod\nimport requests"]), encoding="utf-8")


SINGLE_CASES = {
    "text_kitchen": ["kitchen_sink.ipynb"],
    "json_kitchen": ["kitchen_sink.ipynb", "--format", "json"],
    "output_kitchen": ["kitchen_sink.ipynb", "--output"],
    "output_json_kitchen": ["kitchen_sink.ipynb", "--output", "--format", "json"],
    "outputdir_kitchen": ["kitchen_sink.ipynb", "--output-dir", "out"],
    "outputdir_suffix": ["kitchen_sink.ipynb", "--output-dir", "out", "--suffix", "_x"],
    "inplace_kitchen": ["kitchen_sink.ipynb", "--in-place"],
    "suffix_kitchen": ["kitchen_sink.ipynb", "--output", "--suffix", "_locked"],
    "text_magic": ["magic_sink.ipynb"],
    "json_magic": ["magic_sink.ipynb", "--format", "json"],
    "output_magic": ["magic_sink.ipynb", "--output"],
    "fullfreeze_text": ["kitchen_sink.ipynb", "--full-freeze"],
    "fullfreeze_output": ["kitchen_sink.ipynb", "--output", "--full-freeze"],
    "timeout_text": ["kitchen_sink.ipynb", "--timeout", "5"],
    "quiet_text": ["kitchen_sink.ipynb", "--quiet"],
    "verbose_text": ["kitchen_sink.ipynb", "--verbose"],
    "corrupt_text": ["corrupt.ipynb"],
    "corrupt_json": ["corrupt.ipynb", "--format", "json"],
    "corrupt_output": ["corrupt.ipynb", "--output"],
    "missing_file": ["nope.ipynb"],
    "missing_json": ["nope.ipynb", "--format", "json"],
    "r_notebook": ["rnb.ipynb"],
    "r_json": ["rnb.ipynb", "--format", "json"],
    "tagged_text": ["tagged.ipynb"],
    "tagged_output": ["tagged.ipynb", "--output"],
    "tagged_json": ["tagged.ipynb", "--format", "json"],
    "local_text": ["withlocal.ipynb"],
    "local_output": ["withlocal.ipynb", "--output"],
    "no_args": [],
    "output_no_target": ["--output"],
    "check_drift_no_manifest": ["kitchen_sink.ipynb", "--check-drift"],
    "check_drift_no_manifest_json": ["kitchen_sink.ipynb", "--check-drift", "--format", "json"],
}


def setup_batch(case_dir: Path) -> None:
    root = case_dir / "repo"
    (root / "sub").mkdir(parents=True)
    shutil.copy(FIXTURES / "kitchen_sink.ipynb", root / "kitchen_sink.ipynb")
    shutil.copy(FIXTURES / "magic_sink.ipynb", root / "sub" / "magic_sink.ipynb")
    (root / "localmod.py").write_text("x = 1\n", encoding="utf-8")
    (root / "withlocal.ipynb").write_text(nb(["import localmod\nimport requests"]), encoding="utf-8")
    (root / "tagged.ipynb").write_text(
        nb(["!pip install torch==2.1.0+cu118 --extra-index-url https://download.pytorch.org/whl/cu118\nimport requests"]),
        encoding="utf-8",
    )
    (root / "rnb.ipynb").write_text(nb(["library(ggplot2)"], "ir", "R"), encoding="utf-8")
    (root / "already_merged.ipynb").write_text(nb(["import requests"]), encoding="utf-8")


def setup_batch_bad(case_dir: Path) -> None:
    setup_batch(case_dir)
    (case_dir / "repo" / "corrupt.ipynb").write_text("{ not json", encoding="utf-8")


BATCH_CASES = {
    "batch_text": (["--batch", "repo"], setup_batch),
    "batch_json": (["--batch", "repo", "--format", "json"], setup_batch),
    "batch_positional": (["repo"], setup_batch),
    "batch_output": (["--batch", "repo", "--output"], setup_batch),
    "batch_output_json": (["--batch", "repo", "--output", "--format", "json"], setup_batch),
    "batch_outputdir": (["--batch", "repo", "--output-dir", "out"], setup_batch),
    "batch_outputdir_suffix": (["--batch", "repo", "--output-dir", "out", "--suffix", "_x"], setup_batch),
    "batch_inplace": (["--batch", "repo", "--in-place"], setup_batch),
    "batch_suffix_output": (["--batch", "repo", "--output", "--suffix", "_locked"], setup_batch),
    "batch_universal": (["--batch", "repo", "--universal"], setup_batch),
    "batch_universal_named": (["--batch", "repo", "--universal", "custom.txt", "--output"], setup_batch),
    "batch_universal_json": (["--batch", "repo", "--universal", "--format", "json"], setup_batch),
    "batch_timeout": (["--batch", "repo", "--output", "--timeout", "5"], setup_batch),
    "batch_quiet": (["--batch", "repo", "--output", "--quiet"], setup_batch),
    "batch_fullfreeze": (["--batch", "repo", "--output", "--full-freeze"], setup_batch),
    "batch_check_drift": (["--batch", "repo", "--check-drift"], setup_batch),
    "batch_check_drift_json": (["--batch", "repo", "--check-drift", "--format", "json"], setup_batch),
    "bad_text": (["--batch", "repo"], setup_batch_bad),
    "bad_json": (["--batch", "repo", "--format", "json"], setup_batch_bad),
    "bad_output": (["--batch", "repo", "--output"], setup_batch_bad),
    "bad_output_json": (["--batch", "repo", "--output", "--format", "json"], setup_batch_bad),
    "bad_universal": (["--batch", "repo", "--universal"], setup_batch_bad),
    "bad_inplace": (["--batch", "repo", "--in-place"], setup_batch_bad),
    "missing_dir": (["--batch", "nowhere"], lambda d: None),
    "missing_dir_output": (["--batch", "nowhere", "--output"], lambda d: None),
}


def run(argv, cwd: Path):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    return subprocess.run(
        [sys.executable, "-m", "steady_py", *argv], cwd=cwd, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )


def written_files(case_dir: Path, skip_names) -> dict:
    out = {}
    for f in sorted(case_dir.rglob("*")):
        if not f.is_file() or f.suffix not in (".ipynb", ".txt"):
            continue
        rel = f.relative_to(case_dir).as_posix()
        if f.name in skip_names:
            continue
        out[rel] = norm(f.read_text(encoding="utf-8"), case_dir)
    return out


def print_case(name, argv, rc, stdout, stderr, written):
    print(f"===== {name} =====")
    print(f"argv: {argv}")
    print(f"exit: {rc}")
    print("--- stdout ---")
    print(stdout)
    print("--- stderr ---")
    print(stderr)
    if written:
        print("--- written files ---")
        for rel in sorted(written):
            print(f"  [{rel}]")
            print(written[rel])
    print()


def main():
    tmp_base = Path(tempfile.mkdtemp(prefix="steady_py_cli_baseline_"))
    try:
        for name, argv in SINGLE_CASES.items():
            case_dir = tmp_base / "single" / name
            case_dir.mkdir(parents=True)
            setup_single(case_dir)
            skip = {"kitchen_sink.ipynb", "magic_sink.ipynb", "corrupt.ipynb", "rnb.ipynb", "tagged.ipynb", "withlocal.ipynb"}
            if name in ("inplace_kitchen",):
                skip = set()
            result = run(argv, case_dir)
            print_case(name, argv, result.returncode, norm(result.stdout, case_dir), norm(result.stderr, case_dir),
                      written_files(case_dir, skip))

        for name, (argv, setup) in BATCH_CASES.items():
            case_dir = tmp_base / "batch" / name
            case_dir.mkdir(parents=True)
            setup(case_dir)
            result = run(argv, case_dir)
            print_case(name, argv, result.returncode, norm(result.stdout, case_dir), norm(result.stderr, case_dir),
                      written_files(case_dir, set()))
    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)


if __name__ == "__main__":
    main()
