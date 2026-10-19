"""Copy the corpus fixtures from test_notebooks into tests/fixtures/corpus, outputs stripped.

  python tools/corpus_fixtures.py [--source test_notebooks]

The 33 notebooks were chosen by greedy coverage: every install-line form, option, spec kind and
guard in the corpus's install-line scan, and every warning, notice, dependency status, GPU
framework and install-line kind in the analysis of the 1284-notebook corpus sample. Only sources
whose license allows redistribution are used (ATTRIBUTION.md). Code cell outputs and execution
counts, and notebook widget state, are removed; nothing else is changed.

The fixtures go into a public repository, so code cells are scanned for likely secrets. Hits are
listed and the script exits 1: review each file, replace any real secret with a placeholder, and
note the change in ATTRIBUTION.md before committing.

Stdlib only, so it runs in any venv. Run from the repository root.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

DEST = Path("tests/fixtures/corpus")

# Source directory under test_notebooks -> (where it comes from, license).
SOURCES: Dict[str, Tuple[str, str]] = {
    "huggingface": ("https://github.com/huggingface/notebooks", "Apache-2.0"),
    "cookbook": ("https://github.com/google-gemini/cookbook", "Apache-2.0"),
    "teachopencadd": ("https://github.com/volkamerlab/teachopencadd", "CC-BY-4.0"),
    "datashader": ("https://github.com/holoviz/datashader", "BSD-3-Clause"),
    "handson-ml3": ("https://github.com/ageron/handson-ml3", "Apache-2.0"),
    "sagemaker": ("https://github.com/aws/amazon-sagemaker-examples", "Apache-2.0"),
    "packt": ("https://github.com/PacktPublishing/Data-Strategy-for-LLMs", "MIT"),
    "ISLP_labs": ("https://github.com/intro-stat-learning/ISLP_labs", "BSD-2-Clause"),
    "kaggle_samples": ("Kaggle public notebooks (Meta Kaggle Code)", "Apache-2.0"),
    "kaggle_corpus": ("Kaggle public notebooks (Meta Kaggle Code)", "Apache-2.0"),
    "mine": ("steady-py's author", "steady-py's own"),
}

NOTEBOOKS: List[str] = [
    # Install-line coverage.
    "packt/Data-Strategy-for-LLMs/chapter_03/Jupyter_Notebooks/Chapter_3_Notebook.ipynb",
    "huggingface/examples/onnx-export.ipynb",
    "teachopencadd/teachopencadd/talktorials/T019_md_simulation/talktorial.ipynb",
    "datashader/8_Polygons.ipynb",
    "huggingface/lerobot/training-act.ipynb",
    "sagemaker/sm-async_inference_with_python_sdk.ipynb",
    "huggingface/diffusers/geodiff_molecule_conformation.ipynb",
    "cookbook/quickstarts/Get_started_LyriaRealTime.ipynb",
    "sagemaker/sm-triton_inferentia2.ipynb",
    "handson-ml3/tools_pandas.ipynb",
    "huggingface/longform-qa/Long_Form_Question_Answering_with_ELI5_and_Wikipedia.ipynb",
    "kaggle_samples/failed/spooky-author-identification_jaymanvirk_fine-tuning-logreg-rf-nb-svm-and-xgboost_v7_C1.ipynb",
    "huggingface/course/en/chapter13/grpo_finetune.ipynb",
    "cookbook/examples/langchain/Chat_with_SQL_using_langchain.ipynb",
    "huggingface/examples/timesfm2_5.ipynb",
    "huggingface/diffusers/sd_textual_inversion_training.ipynb",
    "huggingface/diffusers/clip_guided_img2img_stable_diffusion.ipynb",
    "packt/Data-Strategy-for-LLMs/chapter_05/Jupyter_Notebooks/Chapter_5_Notebook.ipynb",
    "sagemaker/sm-pipelines_step_decorator_with_classic_training_step.ipynb",
    "huggingface/diffusers_doc/en/pytorch/weighted_prompts.ipynb",
    "huggingface/peft/gemma_7b_english_quotes.ipynb",
    "huggingface/smolagents_doc/en/inspect_runs.ipynb",
    # Chosen by hand: the author's own cases, a long real competition notebook, a course notebook.
    "mine/medgemma-kaggle.ipynb",
    "mine/silhouette-testing.ipynb",
    "kaggle_samples/failed/plant-pathology-2020-fgvc7_mamamot_fastai-v2-example_v2_C1.ipynb",
    "ISLP_labs/Ch06-varselect-lab.ipynb",
    # Analysis coverage.
    "kaggle_corpus/310110700.ipynb",
    "kaggle_corpus/130818535.ipynb",
    "kaggle_corpus/246556058.ipynb",
    "kaggle_corpus/62497958.ipynb",
    "kaggle_corpus/295962447.ipynb",
    "kaggle_corpus/209669249.ipynb",
    "kaggle_corpus/282973518.ipynb",
]

SECRET_PATTERNS = [
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("GitHub token", re.compile(r"\b(ghp|gho|ghu|ghs)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("hard-coded credential", re.compile(
        r"""(?i)\b\w*(api[_-]?key|token|secret|password|passwd)\w*\s*[:=]\s*["'][^"'\s{}<>]{12,}["']""")),
]


def _source_text(cell: dict) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


def strip(notebook: dict) -> dict:
    """The notebook without code cell outputs, execution counts or widget state."""
    for cell in notebook.get("cells", []):
        if cell.get("cell_type") == "code":
            cell["outputs"] = []
            cell["execution_count"] = None
    if isinstance(notebook.get("metadata"), dict):
        notebook["metadata"].pop("widgets", None)
    return notebook


def secrets(notebook: dict) -> List[Tuple[int, int, str, str]]:
    """(cell index, line index, what it looks like, the line) for each likely secret."""
    hits = []
    for c, cell in enumerate(notebook.get("cells", [])):
        for l, line in enumerate(_source_text(cell).splitlines()):
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    hits.append((c, l, label, line.strip()[:120]))
                    break
    return hits


def attribution(present: List[str]) -> str:
    lines = [
        "# Corpus fixtures: sources and licenses",
        "",
        "Copied by tools/corpus_fixtures.py from steady-py's test corpus. Code cell outputs, execution",
        "counts and widget state were removed; the notebooks are otherwise unchanged unless noted below.",
        "",
    ]
    for group in sorted({p.split("/")[0] for p in present}):
        where, license_name = SOURCES[group]
        lines += [f"## {group}", "", f"Source: {where}. License: {license_name}.", ""]
        lines += [f"- {p}" for p in present if p.split("/")[0] == group] + [""]
    lines += ["## Changes beyond stripping", "", "None.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="test_notebooks", help="the corpus directory (default: test_notebooks)")
    args = parser.parse_args()
    source = Path(args.source)

    missing = [p for p in NOTEBOOKS if not (source / p).is_file()]
    if missing:
        print(f"Not found under {source}:", *(f"  {p}" for p in missing), sep="\n", file=sys.stderr)
        return 1

    for stale in DEST.rglob("*.ipynb"):
        if stale.relative_to(DEST).as_posix() not in NOTEBOOKS:
            stale.unlink()
    flagged = 0
    total = 0
    for rel in NOTEBOOKS:
        with open(source / rel, encoding="utf-8") as fh:
            notebook = strip(json.load(fh))
        target = DEST / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(notebook, indent=1, ensure_ascii=False) + "\n"
        target.write_text(text, encoding="utf-8")
        total += len(text.encode("utf-8"))
        for c, l, label, line in secrets(notebook):
            flagged += 1
            print(f"{rel} cell {c} line {l}: possible {label}: {line}")
    (DEST / "ATTRIBUTION.md").write_text(attribution(NOTEBOOKS), encoding="utf-8")

    print(f"Wrote {len(NOTEBOOKS)} notebooks ({total / 1024:.0f} KiB) and ATTRIBUTION.md to {DEST}.")
    if flagged:
        print(f"{flagged} possible secret(s) above: review them before committing.")
        return 1
    print("No likely secrets found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
