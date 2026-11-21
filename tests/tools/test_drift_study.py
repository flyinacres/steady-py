"""tools/drift_study.py: the first-error classifier, release dating, repin progress, and sampling.
The container half (tools/drift_exec.py) is exercised by the study's own pilot run; here its result
records are built directly."""
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("drift_study", REPO / "tools" / "drift_study.py")
study = importlib.util.module_from_spec(spec)
sys.modules["drift_study"] = study
spec.loader.exec_module(study)
Kind, ex = study.Kind, study.drift_exec


def errored(ename, evalue, frames=(), **extra):
    error = ex.ErrorInfo(cell=3, ename=ename, evalue=evalue, traceback="", frames=list(frames),
                         missing_module=extra.pop("missing_module", ""),
                         missing_top_installed=extra.pop("missing_top_installed", False))
    return ex.Result(python="3.12.7", kernel_python="python3", outcome="error", error=error, **extra)


CASES = [
    ("removed pandas method", errored("AttributeError", "'DataFrame' object has no attribute 'append'", [("pandas", "pandas")]),
     Kind.API, "pandas", "rule"),
    ("numpy 2 against old pandas", errored("ValueError", "numpy.dtype size changed, may indicate binary incompatibility",
                                           [("pandas", "pandas")]), Kind.BINARY, "pandas", "rule"),
    ("data file inside pandas", errored("FileNotFoundError", "No such file: '/kaggle/input/x.csv'", [("pandas", "pandas")]),
     Kind.DATA, "pandas", "rule"),
    ("no GPU", errored("AssertionError", "Torch not compiled with CUDA enabled", [("torch", "torch")]),
     Kind.HARDWARE, "torch", "rule"),
    ("never installed", errored("ModuleNotFoundError", "No module named 'cv2'", missing_module="cv2"),
     Kind.MISSING, "opencv-python", "rule"),
    ("install cell failed first", errored("ModuleNotFoundError", "No module named 'lightgbm'", missing_module="lightgbm",
                                          install_errors=[ex.InstallError(1, ["lightgbm"], "")]),
     Kind.INSTALL, "lightgbm", "rule"),
    ("submodule moved", errored("ModuleNotFoundError", "No module named 'sklearn.externals.joblib'",
                                missing_module="sklearn.externals.joblib", missing_top_installed=True),
     Kind.API, "scikit-learn", "rule"),
    ("name gone", errored("ImportError", "cannot import name 'joblib' from 'sklearn.externals' (/x/site-packages/sklearn/externals/__init__.py)",
                          [("sklearn", "scikit-learn")]), Kind.API, "scikit-learn", "rule"),
    ("module attribute gone", errored("AttributeError", "module 'numpy' has no attribute 'float'"),
     Kind.API, "numpy", "rule"),
    ("only kernel frames", errored("ZeroDivisionError", "division by zero", [("IPython", "ipython")]),
     Kind.CODE, "", "heuristic"),
    ("execution order", errored("NameError", "name 'df' is not defined"), Kind.CODE, "", "heuristic"),
    ("keyboard input", errored("StdinNotImplementedError", "raw_input was called", [("ipykernel", "ipykernel")]),
     Kind.HARNESS, "", "rule"),
    ("requirements without a wheel", ex.Result(python="3.12.7", kernel_python="python3", outcome="requirements_failed",
                                               requirements_failed_names=["pandas"],
                                               setup_tail="ERROR: No matching distribution found for pandas==1.5.3"),
     Kind.INSTALL, "pandas", "rule"),
    ("ran to the end", ex.Result(python="3.12.7", kernel_python="python3"), Kind.COMPLETED, "", "rule"),
]


@pytest.mark.parametrize("result,kind,dist,confidence", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_classify(result, kind, dist, confidence):
    verdict = study.classify(result)
    assert (verdict.kind, verdict.dist, verdict.confidence) == (kind, dist, confidence)


def test_results_survive_their_json_round_trip():
    result = CASES[1][1]
    rebuilt = study.exec_result(json.loads(json.dumps(study.asdict(result))))
    assert rebuilt == result and study.classify(rebuilt).kind == Kind.BINARY


def _file(day, wheel=True, yanked=False):
    return {"upload_time_iso_8601": f"{day}T00:00:00Z", "packagetype": "bdist_wheel" if wheel else "sdist", "yanked": yanked}


def test_dated_release_is_the_newest_final_unyanked_release_before_the_date():
    project = {"releases": {
        "1.4.0": [_file("2022-01-01")],
        "1.5.0": [_file("2022-06-01", wheel=False)],
        "1.5.1": [_file("2022-07-01", yanked=True)],
        "1.6.0rc1": [_file("2022-08-01")],
        "2.0.0": [_file("2023-04-01")],
        "1.4.9": [_file("2023-05-01")],  # a backport, uploaded after the date
    }}
    assert study.dated_release(project, dt.date(2023, 1, 1)) == study.DatedRelease("1.5.0", has_wheels=False)
    assert study.dated_release(project, dt.date(2021, 1, 1)) is None


def test_progress_compares_a_repin_run_with_its_baseline():
    base = CASES[0][1]
    later = errored("KeyError", "'x'")
    later.error.cell = 7
    assert study.progress(base, later, "") == "past_error"
    assert study.progress(base, base, "") == "same_error"
    assert study.progress(base, ex.Result(python="", kernel_python="", outcome="pin_failed", pin_status="no_wheel"), "") == "no_wheel"
    assert study.progress(base, None, "no release of x before 2020-01-01 on PyPI") == "no_dated_release"


def _notebook(path, code, python="3.9.7"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cells": [{"cell_type": "code", "source": code, "metadata": {}, "outputs": []}],
                                "metadata": {"language_info": {"name": "python", "version": python}},
                                "nbformat": 4, "nbformat_minor": 5}), encoding="utf-8")


def _commit(repo, day):
    env = {**os.environ, "GIT_AUTHOR_DATE": f"{day}T12:00:00", "GIT_COMMITTER_DATE": f"{day}T12:00:00"}
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)


def test_sample_dates_full_clones_and_copies_flat_notebooks_alone(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    _notebook(corpus / "packt" / "book" / "ch1" / "old.ipynb", "import pandas")
    (corpus / "packt" / "book" / "requirements.txt").write_text("pandas==1.5.3\nnumpy\n", encoding="utf-8")
    _commit(corpus / "packt" / "book", "2021-03-01")
    _notebook(corpus / "packt" / "book" / "ch1" / "z_copy.ipynb", "import pandas")  # same code: a duplicate
    _notebook(corpus / "kaggle" / "k.ipynb", "import numpy", python="3.10.12")  # undated, old Python: kept
    _notebook(corpus / "kaggle" / "new.ipynb", "import torch", python="3.12.1")  # undated, recent Python: dropped
    monkeypatch.setattr(study, "RUNS", tmp_path / "runs")
    study.main(["sample", "--corpus", str(corpus), "--group", "education=packt", "--group", "kaggle=kaggle"])
    rows = {r.path: r for r in study.read_sample()}
    assert sorted(rows) == ["kaggle/k.ipynb", "packt/book/ch1/old.ipynb"]
    book = rows["packt/book/ch1/old.ipynb"]
    assert (book.root, book.date, book.date_source, book.requirements, book.requirements_pinned) == \
        ("packt/book", "2021-03-01", "git", "requirements.txt", "1/2")
    flat = rows["kaggle/k.ipynb"]
    assert (flat.root, flat.date, flat.date_source) == ("", "", "python_proxy")
