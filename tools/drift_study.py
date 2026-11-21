"""First-error drift study: run notebooks that are at least two years old on today's platforms, record
and classify each one's first error, and test whether pinning the failing package to the release that
was current at the notebook's date gets past it.

Subcommands, run in this order (all write under drift_runs/ at the repository root):
1. sample: picks the notebooks and writes sample.csv. Groups are first-level corpus subdirectories
   (--group education=packt,trainindata --group kaggle=kaggle). A notebook's date is its last git
   commit, read only from full clones (a shallow clone dates every file to the clone), or a --dates
   CSV (path,date). Undated notebooks are kept only when their recorded Python is at most
   --undated-max-python (default 3.10), and then get no repin pass.
2. run: one fresh container per notebook and target (education: python and colab; kaggle: kaggle),
   appending to results.jsonl. The notebook's repository is copied into the container, so the corpus
   is never written to. On the python target the repository's requirements.txt is installed first.
3. repin: for each run whose first error pinning could fix, pins the failing package to its newest
   release before the notebook's date and reruns, up to --rounds times; when that doesn't get past the
   error, one more run adds that release's own dependencies, also dated, as constraints.
4. report: the tables, in report.txt, and review.csv, where kind_reviewed and dist_reviewed correct
   the automatic classification; report reads them back and keeps them on every rewrite.
Every subcommand resumes: a run already in results.jsonl is not repeated.
Host needs the standard library and packaging; the container side is tools/drift_exec.py.
Progress: one line per run, plus the container's line per step and per code cell.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import random
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
RUNS = REPO / "drift_runs"
CORPUS = REPO / "test_notebooks"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while being built
    spec.loader.exec_module(module)
    return module


drift_exec = _load("drift_exec", TOOLS / "drift_exec.py")
run_suite = _load("run_suite", REPO / "tests" / "runners" / "run_suite.py")
IMAGES = {
    "python": "python:3.12-slim",
    "kaggle": run_suite.TIER_CONFIGS["kaggle"].image,
    "colab": run_suite.TIER_CONFIGS["colab"].image,
}
DEFAULT_GROUPS = {"education": ["packt", "trainindata"], "kaggle": ["kaggle"]}
DEFAULT_TARGETS = {"education": ["python", "colab"], "kaggle": ["kaggle"]}
# Distribution names by import name, the reverse of the corpus scanner's table; where several
# distributions provide one import name (cv2), the table's first, the common one, wins.
IMPORT_TO_DIST: Dict[str, str] = {}
for _dist, _imp in _load("corpus_install_scan", TOOLS / "corpus_install_scan.py").ALIASES.items():
    IMPORT_TO_DIST.setdefault(_imp.split(".")[0], _dist)


# ---------------------------------------------------------------- classification

class Kind(str, Enum):
    INSTALL = "install"  # requirements or an install cell failed
    MISSING = "missing_module"  # imported but never installed
    BINARY = "binary_import"  # installed, but its compiled parts don't load against what else is installed
    API = "api_change"  # a third-party package no longer behaves as the code expects
    DATA = "data_resource"  # a file, URL, credential or mounted drive the run can't reach
    HARDWARE = "hardware"  # GPU, CUDA or TPU
    CODE = "notebook_code"  # the notebook's own code, execution order included
    TIMEOUT = "timeout"
    CRASH = "crash"  # the kernel died
    HARNESS = "harness"  # this study's setup failed, or the notebook wanted keyboard input
    COMPLETED = "completed"
    OTHER = "other"


PINNABLE = {Kind.INSTALL, Kind.MISSING, Kind.BINARY, Kind.API}
BLOCKED = {Kind.DATA, Kind.HARDWARE, Kind.HARNESS, Kind.TIMEOUT, Kind.CRASH}

HARDWARE_TEXT = re.compile(r"CUDA|cuda|cuDNN|cudnn|NVIDIA|nvidia|\bGPU|\bTPU|\bXLA\b|device-side assert|ROCm")
BINARY_TEXT = re.compile(
    r"numpy\.dtype size changed|_ARRAY_API not found|compiled using NumPy 1\.x|binary incompatib|undefined symbol"
    r"|cannot open shared object file|DLL load failed|module compiled against (?:ABI|API) version"
    r"|numpy\.core\.multiarray failed to import|Illegal instruction|GLIBC_|wrong ELF class")
DATA_ENAMES = {"FileNotFoundError", "IsADirectoryError", "NotADirectoryError", "URLError", "HTTPError", "ConnectionError",
               "ConnectTimeout", "ReadTimeout", "gaierror", "SSLError", "ProxyError", "MaxRetryError", "NewConnectionError",
               "ConnectionRefusedError", "TimeoutError", "KaggleApiHTTPError", "GatedRepoError", "RepositoryNotFoundError",
               "LocalEntryNotFoundError", "OfflineModeIsEnabled", "MessageError", "DefaultCredentialsError"}
DATA_TEXT = re.compile(r"No such file or directory|/kaggle/input|does not exist|api[_ ]?key|API key|credentials|"
                       r"HTTP Error \d|\b40[134]\b|Unauthorized|Forbidden|token|drive\.mount|mountpoint|authenticate",
                       re.IGNORECASE)
API_TEXT = re.compile(r"has no attribute|unexpected keyword argument|got an unexpected|positional argument|"
                      r"cannot import name|deprecated|was removed|has been removed|no longer|renamed|is not supported"
                      r"|not a valid value|must be one of|Expected .* got|invalid value")
# The kernel's own machinery shows in most tracebacks; a frame there says nothing about the notebook's packages.
KERNEL_MODULES = {"IPython", "ipykernel", "jupyter_client", "traitlets", "tornado", "zmq", "comm", "debugpy", "google"}
MODULE_ATTR = re.compile(r"module '([A-Za-z0-9_.]+)' has no attribute")


@dataclass(frozen=True)
class Verdict:
    kind: Kind
    dist: str  # the distribution the first error points at, or ""
    confidence: str  # rule: a specific pattern matched; heuristic: a judgment worth reviewing
    reason: str


def dist_for_module(module: str) -> str:
    top = module.split(".")[0]
    return IMPORT_TO_DIST.get(top, top)


def classify(result: Any) -> Verdict:
    """The first error's kind, from a drift_exec.Result. Patterns are checked from the most specific
    (setup outcomes, hardware, binary loading, data) to the least (anything inside a package)."""
    outcome = result.outcome
    if outcome == "completed":
        return Verdict(Kind.COMPLETED, "", "rule", "ran to the end")
    if outcome == "requirements_failed":
        names = result.requirements_failed_names
        why = "no wheel or release for this Python" if "No matching distribution" in result.setup_tail else "pip failed"
        return Verdict(Kind.INSTALL, names[0] if names else "", "rule", f"requirements: {why}")
    if outcome in ("pin_failed", "setup_failed"):
        return Verdict(Kind.HARNESS, "", "rule", outcome)
    if outcome in ("timeout", "notebook_timeout"):
        return Verdict(Kind.TIMEOUT, "", "rule", outcome)
    if outcome == "kernel_died":
        return Verdict(Kind.CRASH, "", "heuristic", "kernel died (memory, or a crash in compiled code)")
    error = result.error
    if error is None:
        return Verdict(Kind.OTHER, "", "heuristic", f"outcome {outcome} without an error")
    text = f"{error.ename}: {error.evalue}"
    first = next((d for m, d in error.frames if d and m not in KERNEL_MODULES), "")
    if error.ename == "StdinNotImplementedError":
        return Verdict(Kind.HARNESS, "", "rule", "the notebook asks for keyboard input")
    if HARDWARE_TEXT.search(text):
        return Verdict(Kind.HARDWARE, first, "rule", "hardware named in the error")
    if BINARY_TEXT.search(text):
        return Verdict(Kind.BINARY, first, "rule", "compiled code failed to load")
    if error.ename in DATA_ENAMES or (error.ename in ("OSError", "ValueError", "KeyError", "Exception") and DATA_TEXT.search(text)):
        return Verdict(Kind.DATA, first, "rule" if error.ename in DATA_ENAMES else "heuristic", "a resource the run can't reach")
    if error.ename == "ModuleNotFoundError":
        module = error.missing_module
        if error.missing_top_installed:
            return Verdict(Kind.API, dist_for_module(module), "rule", f"{module} is gone from an installed package")
        failed = {canonicalize_name(n) for e in result.install_errors for n in e.names}
        dist = dist_for_module(module)
        if result.install_errors and (not failed or canonicalize_name(dist) in failed):
            return Verdict(Kind.INSTALL, dist, "rule" if failed else "heuristic", "an install cell failed before this import")
        return Verdict(Kind.MISSING, dist, "rule", f"{module} is not installed")
    if error.ename == "ImportError" and "cannot import name" in error.evalue:
        return Verdict(Kind.API, first or _module_in(error.evalue), "rule", "a name is gone from a package")
    if error.ename == "NameError":
        return Verdict(Kind.CODE, "", "heuristic", "an undefined name (execution order, or a name an import * no longer provides)")
    module_attr = MODULE_ATTR.search(error.evalue)
    if error.ename == "AttributeError" and module_attr and not first:
        return Verdict(Kind.API, dist_for_module(module_attr.group(1)), "rule", "a module attribute is gone")
    if first:
        confidence = "rule" if API_TEXT.search(text) else "heuristic"
        return Verdict(Kind.API, first, confidence, "raised inside a package")
    if error.ename in ("AttributeError", "TypeError") and API_TEXT.search(text):
        return Verdict(Kind.API, "", "heuristic", "API-like message raised in the notebook's own frame")
    return Verdict(Kind.CODE, "", "heuristic", "raised in the notebook's own code")


def _module_in(evalue: str) -> str:
    found = re.search(r"from '([A-Za-z0-9_.]+)'", evalue)
    return dist_for_module(found.group(1)) if found else ""


# ---------------------------------------------------------------- sampling

@dataclass
class SampleRow:
    path: str  # the notebook, relative to the corpus directory, / separated
    group: str
    root: str  # the directory copied into the container (the notebook's repository), relative to the corpus; "" copies the notebook alone
    date: str  # ISO date of the notebook's last commit, or ""
    date_source: str  # git, dates_file, python_proxy
    authored_python: str  # major.minor from the notebook's metadata, or ""
    requirements: str  # the nearest requirements.txt, relative to root, or ""
    requirements_pinned: str  # requirement lines pinned with ==, as "pinned/total"
    code_cells: int


@dataclass(frozen=True)
class NotebookInfo:
    python: str
    code_hash: str
    code_cells: int


def notebook_info(path: Path) -> Optional[NotebookInfo]:
    """None for a notebook that doesn't parse, isn't Python, or has no code."""
    try:
        nb = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    meta = nb.get("metadata", {}) if isinstance(nb, dict) else {}
    language = (meta.get("kernelspec", {}).get("language") or meta.get("language_info", {}).get("name") or "python")
    if str(language).lower() != "python":
        return None
    code = ["".join(c.get("source", "")) for c in nb.get("cells", []) if c.get("cell_type") == "code"]
    if not any(s.strip() for s in code):
        return None
    version = str(meta.get("language_info", {}).get("version", ""))
    python = ".".join(version.split(".")[:2]) if re.match(r"\d+\.\d+", version) else ""
    return NotebookInfo(python, hashlib.sha1("\n".join(code).encode()).hexdigest(), len(code))


def find_root(notebook: Path, corpus: Path) -> Tuple[Optional[Path], bool]:
    """The notebook's repository and whether it is a git clone: the nearest parent holding .git, below
    the corpus directory; otherwise the corpus subdirectory's child that holds the notebook. None for a
    notebook directly in a corpus subdirectory (a flat set like kaggle), which is copied alone."""
    for parent in notebook.parents:
        if parent == corpus:
            break
        if (parent / ".git").exists():
            return parent, True
    parts = notebook.relative_to(corpus).parts
    return (corpus / parts[0] / parts[1], False) if len(parts) >= 3 else (None, False)


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8")
    return done.stdout.strip() if done.returncode == 0 else ""


def requirements_for(notebook: Path, root: Path) -> Tuple[str, str]:
    for parent in [notebook.parent, *notebook.parent.parents]:
        candidate = parent / "requirements.txt"
        if candidate.is_file():
            lines = [ln.split("#")[0].strip() for ln in candidate.read_text(encoding="utf-8", errors="replace").splitlines()]
            reqs = [ln for ln in lines if ln and not ln.startswith("-")]
            pinned = sum(1 for ln in reqs if "==" in ln)
            return candidate.relative_to(root).as_posix(), f"{pinned}/{len(reqs)}"
        if parent == root:
            break
    return "", ""


def python_tuple(text: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in text.split(".") if p.isdigit())


def cmd_sample(args: argparse.Namespace) -> None:
    corpus = Path(args.corpus).resolve()
    groups = parse_mapping(args.group, DEFAULT_GROUPS)
    by_subdir = {sub: name for name, subs in groups.items() for sub in subs}
    dates = read_dates(args.dates) if args.dates else {}
    cutoff = dt.date.today() - dt.timedelta(days=round(365.25 * args.min_age_years))
    shallow: Dict[Path, bool] = {}
    candidates: Dict[str, List[SampleRow]] = {g: [] for g in groups}
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    subdirs = Counter(p.relative_to(corpus).parts[0] for p in corpus.rglob("*.ipynb"))
    print("Notebooks per corpus subdirectory: " + ", ".join(f"{k} {v}" for k, v in sorted(subdirs.items())))
    for n, path in enumerate(sorted(corpus.rglob("*.ipynb")), start=1):
        if n % 100 == 0:
            print(".", end="", flush=True)
        rel = path.relative_to(corpus)
        group = by_subdir.get(rel.parts[0])
        if group is None or ".ipynb_checkpoints" in rel.parts:
            continue
        info = notebook_info(path)
        if info is None:
            counts["skipped: unparseable, not Python, or no code"] += 1
            continue
        if info.code_hash in seen:
            counts["skipped: duplicate code"] += 1
            continue
        seen.add(info.code_hash)
        root, is_git = find_root(path, corpus)
        date, source = dates.get(rel.as_posix(), ""), "dates_file"
        if not date and is_git and root is not None:
            if root not in shallow:
                shallow[root] = git(root, "rev-parse", "--is-shallow-repository") == "true"
            if not shallow[root]:
                date, source = git(root, "log", "-1", "--format=%cs", "--", str(path.relative_to(root)))[:10], "git"
        if date:
            if dt.date.fromisoformat(date) > cutoff:
                counts["skipped: newer than the cutoff"] += 1
                continue
        elif info.python and python_tuple(info.python) <= python_tuple(args.undated_max_python):
            source = "python_proxy"
        else:
            counts["skipped: no date, and no old enough Python recorded"] += 1
            continue
        counts[f"{group}: candidate ({source})"] += 1
        requirements, pinned = requirements_for(path, root) if root else ("", "")
        candidates[group].append(SampleRow(rel.as_posix(), group, root.relative_to(corpus).as_posix() if root else "", date, source,
                                           info.python, requirements, pinned, info.code_cells))
    print()
    rng = random.Random(args.seed)
    chosen: List[SampleRow] = []
    for group, rows in candidates.items():
        picked = rng.sample(rows, min(args.per_group, len(rows)))
        chosen += picked
        print(f"{group}: {len(picked)} chosen of {len(rows)} candidates")
    for key, value in sorted(counts.items()):
        print(f"  {key}: {value}")
    if any(not rows for rows in candidates.values()):
        print("A group has no candidates: check --group, or supply dates with --dates.")
    shallow_roots = sorted(r.relative_to(corpus).as_posix() for r, s in shallow.items() if s)
    if shallow_roots:
        print(f"Shallow clones (no dates read; `git fetch --unshallow` in each to date them): {', '.join(shallow_roots)}")
    RUNS.mkdir(exist_ok=True)
    write_rows(RUNS / "sample.csv", chosen)
    print(f"Wrote {RUNS / 'sample.csv'}")


def read_dates(path: str) -> Dict[str, str]:
    with open(path, encoding="utf-8", newline="") as f:
        return {row["path"].replace("\\", "/"): row["date"][:10] for row in csv.DictReader(f)}


def parse_mapping(values: Optional[Sequence[str]], default: Dict[str, List[str]]) -> Dict[str, List[str]]:
    if not values:
        return default
    mapping: Dict[str, List[str]] = {}
    for value in values:
        name, _, items = value.partition("=")
        mapping[name] = [i for i in items.split(",") if i]
    return mapping


def write_rows(path: Path, rows: Sequence[SampleRow]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[fl.name for fl in fields(SampleRow)])
        writer.writeheader()
        writer.writerows(asdict(r) for r in rows)


def read_sample() -> List[SampleRow]:
    with (RUNS / "sample.csv").open(encoding="utf-8", newline="") as f:
        return [SampleRow(**{**row, "code_cells": int(row["code_cells"])}) for row in csv.DictReader(f)]  # type: ignore[arg-type]


# ---------------------------------------------------------------- running

@dataclass
class Job:
    stage: str  # baseline, repin, closure
    row: SampleRow
    target: str
    pins: List[str] = field(default_factory=list)
    sdist_ok: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    baseline_id: str = ""

    @property
    def run_id(self) -> str:
        extra = "+".join(self.pins) + (" -c" if self.constraints else "")
        return f"{self.stage}|{self.target}|{self.row.path}|{extra}"


@dataclass
class RunRecord:
    run_id: str
    stage: str
    target: str
    image: str
    image_created: str
    path: str
    group: str
    date: str
    pins: List[str]
    constraints: List[str]
    baseline_id: str
    result: Optional[Dict[str, Any]]  # drift_exec.Result as written, or None when the container wrote none
    note: str = ""  # why there is no result: the container's last lines, or a repin that couldn't be set up


def exec_result(data: Dict[str, Any]) -> Any:
    """A drift_exec.Result rebuilt from its JSON."""
    error = data.get("error")
    return drift_exec.Result(**{
        **data,
        "error": drift_exec.ErrorInfo(**{**error, "frames": [tuple(f) for f in error["frames"]]}) if error else None,
        "install_errors": [drift_exec.InstallError(**e) for e in data.get("install_errors", [])],
    })


def result_of(record: RunRecord) -> Any:
    """The record's drift_exec.Result; only for records known to have one."""
    if record.result is None:
        raise ValueError(f"{record.run_id} has no result")
    return exec_result(record.result)


def verdict_of(record: RunRecord) -> Verdict:
    if record.result is None:
        return Verdict(Kind.HARNESS, "", "rule", record.note.splitlines()[-1] if record.note else "no result")
    return classify(result_of(record))


def load_records() -> Dict[str, RunRecord]:
    """Every record in results.jsonl, the last one written for each run_id winning."""
    path = RUNS / "results.jsonl"
    records: Dict[str, RunRecord] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = RunRecord(**json.loads(line))
                records[record.run_id] = record
    return records


def append_record(record: RunRecord) -> None:
    RUNS.mkdir(exist_ok=True)
    with (RUNS / "results.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record)) + "\n")


_IMAGE_DATES: Dict[str, str] = {}


def prepare_image(image: str, pull: bool) -> str:
    if image not in _IMAGE_DATES:
        if pull:
            run_suite.refresh_image(image)
        done = subprocess.run(["docker", "image", "inspect", "-f", "{{.Created}}", image], capture_output=True, text=True)
        if done.returncode != 0:
            raise SystemExit(f"Image {image} is not available; run without --no-pull to pull it.")
        _IMAGE_DATES[image] = done.stdout.strip()
    return _IMAGE_DATES[image]


def docker_command(job: Job, args: argparse.Namespace, out_dir: Path) -> Tuple[str, List[str]]:
    """The container's name and the docker command for one job."""
    corpus = Path(args.corpus).resolve()
    notebook = corpus / job.row.path
    root = corpus / job.row.root if job.row.root else notebook.parent
    exec_args = ["--notebook", notebook.name, "--out", "/out/result.json",
                 "--cell-timeout", str(args.cell_timeout), "--notebook-timeout", str(args.notebook_timeout)]
    if job.row.requirements and job.target == "python":
        exec_args += ["--requirements", "/work/" + job.row.requirements]
    for pin in job.pins:
        exec_args += ["--pin", pin]
    for name in job.sdist_ok:
        exec_args += ["--sdist-ok", name]
    if job.constraints:
        exec_args += ["--constraints", "/out/constraints.txt"]
    work_dir = "/work/" + notebook.parent.relative_to(root).as_posix()
    copy = "cp -a /src/. /work/" if job.row.root else f"cp -a {shlex.quote('/src/' + notebook.name)} /work/"
    script = (f"set -e; mkdir -p /work; {copy}; cd {shlex.quote(work_dir)}; "
              f"python3 /tools/drift_exec.py {' '.join(shlex.quote(a) for a in exec_args)}")
    name = "drift-" + hashlib.sha1(job.run_id.encode()).hexdigest()[:12]
    # --mount, not -v: a Windows drive letter's colon would split a -v argument
    return name, ["docker", "run", "--rm", "--name", name,
                  "--mount", f"type=bind,source={root},target=/src,readonly",
                  "--mount", f"type=bind,source={out_dir.resolve()},target=/out",
                  "--mount", f"type=bind,source={TOOLS},target=/tools,readonly",
                  "-e", "PYTHONUNBUFFERED=1", "-e", "PIP_ROOT_USER_ACTION=ignore", "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1",
                  "--entrypoint", "/bin/bash", IMAGES[job.target], "-c", script]


def run_job(job: Job, args: argparse.Namespace) -> RunRecord:
    image = IMAGES[job.target]
    created = prepare_image(image, not args.no_pull)
    out_dir = RUNS / "scratch" / hashlib.sha1(job.run_id.encode()).hexdigest()[:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    result_file = out_dir / "result.json"
    result_file.unlink(missing_ok=True)
    if job.constraints:
        (out_dir / "constraints.txt").write_text("\n".join(job.constraints) + "\n", encoding="utf-8")
    name, cmd = docker_command(job, args, out_dir)
    last: Deque[str] = deque(maxlen=40)
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace", bufsize=1)
    killer = threading.Timer(args.notebook_timeout + 1800, lambda: subprocess.run(["docker", "kill", name], capture_output=True))
    killer.start()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            last.append(line.rstrip())
        process.wait()
    finally:
        killer.cancel()
    result = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else None
    return RunRecord(job.run_id, job.stage, job.target, image, created, job.row.path, job.row.group, job.row.date,
                     job.pins, job.constraints, job.baseline_id, result,
                     "" if result else f"docker exit {process.returncode}\n" + "\n".join(last))


def run_or_reuse(job: Job, args: argparse.Namespace, records: Dict[str, RunRecord], label: str) -> RunRecord:
    if job.run_id in records:
        return records[job.run_id]
    print(f"{label} {job.stage} {job.row.path} on {job.target}{' with ' + ', '.join(job.pins) if job.pins else ''}", flush=True)
    started = time.monotonic()
    record = run_job(job, args)
    append_record(record)
    records[job.run_id] = record
    v = verdict_of(record)
    print(f"  -> {v.kind.value}{' (' + v.dist + ')' if v.dist else ''}: {v.reason} [{time.monotonic() - started:.0f}s]", flush=True)
    return record


def chosen_rows(args: argparse.Namespace) -> List[SampleRow]:
    rows = read_sample()
    if args.only:
        rows = [r for r in rows if r.path in set(args.only)]
    if args.limit:  # alternate the groups, so a pilot covers each
        by_group: Dict[str, List[SampleRow]] = {}
        for r in rows:
            by_group.setdefault(r.group, []).append(r)
        interleaved = [r for batch in zip_longest_rows(list(by_group.values())) for r in batch]
        rows = interleaved[:args.limit]
    return rows


def zip_longest_rows(groups: List[List[SampleRow]]) -> Iterable[List[SampleRow]]:
    for i in range(max((len(g) for g in groups), default=0)):
        yield [g[i] for g in groups if i < len(g)]


def cmd_run(args: argparse.Namespace) -> None:
    targets = parse_mapping(args.targets, DEFAULT_TARGETS)
    jobs = [Job("baseline", row, t) for row in chosen_rows(args) for t in targets.get(row.group, [])]
    records = load_records()
    for n, job in enumerate(jobs, start=1):
        run_or_reuse(job, args, records, f"[{n}/{len(jobs)}]")
    print(f"Done: {len(jobs)} runs recorded in {RUNS / 'results.jsonl'}. Next: repin, then report.")


# ---------------------------------------------------------------- repinning

@dataclass(frozen=True)
class DatedRelease:
    version: str
    has_wheels: bool


def pypi_json(name: str, version: str = "") -> Optional[Dict[str, Any]]:
    """PyPI's JSON for a project, or for one of its releases; cached under drift_runs/pypi_cache."""
    key = canonicalize_name(name) + (f"-{version}" if version else "")
    cache = RUNS / "pypi_cache" / f"{key}.json"
    if cache.exists():
        data: Dict[str, Any] = json.loads(cache.read_text(encoding="utf-8"))
        return data or None
    url = f"https://pypi.org/pypi/{urllib.parse.quote(name)}/{urllib.parse.quote(version) + '/' if version else ''}json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        data = {}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data or None


def dated_release(project: Dict[str, Any], before: dt.date) -> Optional[DatedRelease]:
    """The newest final, unyanked release whose first file was uploaded before `before`."""
    best: Optional[Tuple[Version, DatedRelease]] = None
    for text, files in project.get("releases", {}).items():
        live = [f for f in files if not f.get("yanked")]
        if not live:
            continue
        try:
            version = Version(text)
        except InvalidVersion:
            continue
        uploaded = min(dt.date.fromisoformat(f["upload_time_iso_8601"][:10]) for f in live)
        if version.is_prerelease or version.is_devrelease or uploaded >= before:
            continue
        if best is None or version > best[0]:
            best = (version, DatedRelease(text, any(f.get("packagetype") == "bdist_wheel" for f in live)))
    return best[1] if best else None


def marker_environment(python: str) -> Dict[str, str]:
    """The containers' platform: Linux on x86-64, with the Python the baseline run saw."""
    return {"python_version": ".".join(python.split(".")[:2]), "python_full_version": python, "sys_platform": "linux",
            "platform_system": "Linux", "os_name": "posix", "platform_machine": "x86_64",
            "implementation_name": "cpython", "platform_python_implementation": "CPython", "extra": ""}


def closure_constraints(pins: Sequence[str], before: dt.date, python: str) -> List[str]:
    """Each pinned release's own dependencies, at their newest release before `before`. One level deep:
    enough for the case this tests (a pinned pandas and the numpy it was built against)."""
    pinned = {canonicalize_name(p.split("==")[0]) for p in pins}
    found: Dict[str, str] = {}
    for pin in pins:
        name, _, version = pin.partition("==")
        release = pypi_json(name, version)
        for line in (release or {}).get("info", {}).get("requires_dist") or []:
            try:
                req = Requirement(line)
            except InvalidRequirement:
                continue
            if req.marker is not None and not req.marker.evaluate(marker_environment(python)):
                continue
            dep = canonicalize_name(req.name)
            project = pypi_json(req.name) if dep not in pinned and dep not in found else None
            dated = dated_release(project, before) if project else None
            if dated and req.specifier.contains(dated.version, prereleases=True):
                found[dep] = f"{dep}=={dated.version}"
    return sorted(found.values())


def progress(baseline: Any, after: Optional[Any], note: str) -> str:
    """How a repin run compares with the baseline run of the same notebook and target."""
    if after is None:
        return "no_dated_release" if note.startswith("no release") else "harness"
    if after.outcome == "pin_failed":
        return "no_wheel" if after.pin_status == "no_wheel" else "pin_install_failed"
    if after.outcome == "completed":
        return "completed"
    if after.error is None or baseline.error is None:
        return str(after.outcome)
    if after.error.cell > baseline.error.cell:
        return "past_error"
    return "same_error" if after.error.ename == baseline.error.ename and after.error.cell == baseline.error.cell else "other_error"


HELPED = {"completed", "past_error"}


def cmd_repin(args: argparse.Namespace) -> None:
    records = load_records()
    rows = {r.path: r for r in read_sample()}
    baselines = [r for r in records.values() if r.stage == "baseline" and r.result and r.path in rows]
    eligible = [b for b in baselines if verdict_of(b).kind in PINNABLE and verdict_of(b).dist
                and result_of(b).outcome != "requirements_failed" and rows[b.path].date]
    print(f"{len(eligible)} of {len(baselines)} baseline runs are eligible (a pinnable first error naming a package, and a dated notebook)")
    for n, base in enumerate(eligible, start=1):
        row, label = rows[base.path], f"[{n}/{len(eligible)}]"
        before = dt.date.fromisoformat(row.date)
        python = result_of(base).python
        pins: List[str] = []
        sdist: List[str] = []
        current = base
        for _ in range(args.rounds):
            if current.result is None:
                break
            verdict = verdict_of(current)
            if verdict.kind not in PINNABLE or not verdict.dist:
                break
            if any(canonicalize_name(p.split("==")[0]) == canonicalize_name(verdict.dist) for p in pins):
                break  # already pinned, and the error is still there
            project = pypi_json(verdict.dist)
            release = dated_release(project, before) if project else None
            if release is None:
                note = f"no release of {verdict.dist} before {row.date} on PyPI"
                job = Job("repin", row, base.target, pins + [f"{verdict.dist}==?"], baseline_id=base.run_id)
                if job.run_id not in records:
                    record = RunRecord(job.run_id, "repin", base.target, base.image, base.image_created, row.path, row.group,
                                       row.date, job.pins, [], base.run_id, None, note)
                    append_record(record)
                    records[job.run_id] = record
                    print(f"{label} {note}")
                break
            pins = pins + [f"{verdict.dist}=={release.version}"]
            sdist = sdist + ([verdict.dist] if not release.has_wheels else [])
            current = run_or_reuse(Job("repin", row, base.target, pins, sdist, baseline_id=base.run_id), args, records, label)
            if current.result and result_of(current).outcome == "pin_failed":
                break
        if not pins or args.no_closure or current.result is None:
            continue
        outcome = progress(result_of(base), result_of(current), current.note)
        if outcome in HELPED or outcome == "no_wheel":
            continue
        constraints = closure_constraints(pins, before, python)
        if constraints:
            run_or_reuse(Job("closure", row, base.target, pins, sdist, constraints, base.run_id), args, records, label)


# ---------------------------------------------------------------- reporting

REVIEW_FIELDS = ["run_id", "path", "target", "cell", "ename", "evalue", "kind", "dist", "confidence", "reason",
                 "kind_reviewed", "dist_reviewed", "comment"]


def read_reviews() -> Dict[str, Dict[str, str]]:
    path = RUNS / "review.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {row["run_id"]: row for row in csv.DictReader(f)}


def reviewed(record: RunRecord, reviews: Dict[str, Dict[str, str]]) -> Verdict:
    auto = verdict_of(record)
    row = reviews.get(record.run_id, {})
    kind = Kind(row["kind_reviewed"]) if row.get("kind_reviewed") else auto.kind
    dist = row.get("dist_reviewed") or auto.dist
    return Verdict(kind, dist, "reviewed" if row.get("kind_reviewed") or row.get("dist_reviewed") else auto.confidence, auto.reason)


def write_reviews(baselines: Sequence[RunRecord], reviews: Dict[str, Dict[str, str]]) -> None:
    with (RUNS / "review.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        for record in baselines:
            v = verdict_of(record)
            error = result_of(record).error if record.result else None
            kept = reviews.get(record.run_id, {})
            writer.writerow({"run_id": record.run_id, "path": record.path, "target": record.target,
                             "cell": error.cell if error else "", "ename": error.ename if error else "",
                             "evalue": (error.evalue if error else record.note)[:200].replace("\n", " "),
                             "kind": v.kind.value, "dist": v.dist, "confidence": v.confidence, "reason": v.reason,
                             "kind_reviewed": kept.get("kind_reviewed", ""), "dist_reviewed": kept.get("dist_reviewed", ""),
                             "comment": kept.get("comment", "")})


def pct(n: int, total: int) -> str:
    return f"{n:4d}  {100 * n / total:5.1f}%" if total else f"{n:4d}"


def cmd_report(args: argparse.Namespace) -> None:
    records = load_records()
    reviews = read_reviews()
    baselines = [r for r in records.values() if r.stage == "baseline"]
    write_reviews(baselines, reviews)
    lines: List[str] = []
    images = sorted({(r.target, r.image, r.image_created) for r in baselines})
    lines += ["Images:"] + [f"  {t}: {i}, created {c}" for t, i, c in images]
    for group, target in sorted({(r.group, r.target) for r in baselines}):
        runs = [r for r in baselines if r.group == group and r.target == target]
        verdicts = {r.run_id: reviewed(r, reviews) for r in runs}
        pythons = sorted({result_of(r).python for r in runs if r.result})
        lines += ["", f"== {group} on {target} (Python {', '.join(pythons)}): {len(runs)} notebooks", "First error:"]
        kinds = Counter(v.kind for v in verdicts.values())
        for kind in Kind:
            if kinds[kind]:
                lines.append(f"  {kind.value:<16}{pct(kinds[kind], len(runs))}")
        blocked = sum(kinds[k] for k in BLOCKED)
        pinnable = sum(kinds[k] for k in PINNABLE)
        lines.append(f"  blocked before the code was tested (data, hardware, harness, timeout, crash): {pct(blocked, len(runs)).strip()}")
        lines.append(f"  pinning could address (install, missing, binary, API): {pct(pinnable, len(runs)).strip()}")
        unreviewed = sum(1 for v in verdicts.values() if v.confidence == "heuristic")
        lines.append(f"  heuristic classifications not yet reviewed in review.csv: {unreviewed}")
        failed_reqs = [r for r in runs if r.result and r.result["outcome"] == "requirements_failed"]
        if failed_reqs:
            no_wheel = sum(1 for r in failed_reqs if "No matching distribution" in result_of(r).setup_tail)
            lines.append(f"  requirements.txt failed: {len(failed_reqs)} ({no_wheel} for want of a release or wheel for this Python)")
        api = Counter(v.dist or "(unknown)" for v in verdicts.values() if v.kind == Kind.API)
        if api:
            lines.append("  API changes by package: " + ", ".join(f"{d} {n}" for d, n in api.most_common(10)))
        lines += repin_lines(runs, records)
    report = "\n".join(lines)
    print(report)
    (RUNS / "report.txt").write_text(report + "\n", encoding="utf-8")
    print(f"\nWrote {RUNS / 'report.txt'} and {RUNS / 'review.csv'}")


def repin_lines(runs: Sequence[RunRecord], records: Dict[str, RunRecord]) -> List[str]:
    """Each repinned notebook's final repin run, and closure run, against its baseline."""
    direct: Counter[str] = Counter()
    closure: Counter[str] = Counter()
    for base in runs:
        chain = [r for r in records.values() if r.baseline_id == base.run_id]
        repins = [r for r in chain if r.stage == "repin"]
        if not repins or base.result is None:
            continue
        final = repins[-1]
        direct[progress(result_of(base), result_of(final) if final.result else None, final.note)] += 1
        for r in chain:
            if r.stage == "closure":
                closure[progress(result_of(base), result_of(r) if r.result else None, r.note)] += 1
    if not direct:
        return []
    total = sum(direct.values())
    out = [f"Repinning the failing package to its release before the notebook's date ({total} notebooks):"]
    out += [f"  {k:<20}{pct(v, total)}" for k, v in direct.most_common()]
    out.append(f"  helped (completed or got past the error): {pct(sum(direct[k] for k in HELPED), total).strip()}")
    if closure:
        out.append(f"Adding that release's dated dependencies as constraints, where pinning alone didn't help ({sum(closure.values())}):")
        out += [f"  {k:<20}{pct(v, sum(closure.values()))}" for k, v in closure.most_common()]
    return out


# ---------------------------------------------------------------- command line

def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sample = sub.add_parser("sample", help="pick the notebooks")
    sample.add_argument("--corpus", default=str(CORPUS))
    sample.add_argument("--group", action="append", help="NAME=SUBDIR[,SUBDIR]; repeat per group")
    sample.add_argument("--per-group", type=int, default=25)
    sample.add_argument("--min-age-years", type=float, default=2.0)
    sample.add_argument("--undated-max-python", default="3.10")
    sample.add_argument("--dates", help="CSV with path (relative to the corpus) and date columns")
    sample.add_argument("--seed", type=int, default=2026)
    for name, text in (("run", "baseline runs"), ("repin", "rerun pinnable failures with the failing package pinned")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--corpus", default=str(CORPUS))
        p.add_argument("--cell-timeout", type=int, default=600)
        p.add_argument("--notebook-timeout", type=int, default=1800)
        p.add_argument("--no-pull", action="store_true", help="use the cached images instead of pulling")
        if name == "run":
            p.add_argument("--targets", action="append", help="GROUP=TARGET[,TARGET]; targets: " + ", ".join(IMAGES))
            p.add_argument("--limit", type=int, default=0, help="run only the first N sampled notebooks, alternating groups")
            p.add_argument("--only", action="append", help="run only this notebook (its path in sample.csv)")
        else:
            p.add_argument("--rounds", type=int, default=2, help="packages to pin, one per rerun, before giving up")
            p.add_argument("--no-closure", action="store_true")
    sub.add_parser("report", help="write report.txt and review.csv")
    args = parser.parse_args(argv)
    {"sample": cmd_sample, "run": cmd_run, "repin": cmd_repin, "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    main()
