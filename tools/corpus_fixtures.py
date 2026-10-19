"""Build the corpus fixtures, and say what they cover.

  python tools/corpus_fixtures.py [--source test_notebooks]   copy the notebooks, then write the docs
  python tools/corpus_fixtures.py --docs                       rewrite README.md and ATTRIBUTION.md only
  python tools/corpus_fixtures.py --coverage                   what the fixtures exhibit, and the gaps

This script is the source of truth for tests/fixtures/corpus: which notebooks are there and why
(NOTEBOOKS, a reason per entry), where they come from (SOURCES), and how they were changed. The
notebooks were chosen by greedy coverage: every install-line form, option, spec kind and guard in
the corpus's install-line scan, and every warning, notice, dependency status, GPU framework and
install-line kind in the analysis of the 1284-notebook corpus sample; a few were picked by hand.
Only sources whose license allows redistribution are used. Code cell outputs and execution counts,
and notebook widget state, are removed; nothing else is changed.

The fixtures go into a public repository, so code cells are scanned for likely secrets. Hits are
listed and the script exits 1: review each file, replace any real secret with a placeholder, and
note the change under "Changes beyond stripping" in CHANGES below before committing.

The reasons come from the scans the selection used (the corpus install-line scan, which is
independent of steady-py, and the corpus sample's analysis). --coverage reads the golden file
(tests/fixtures/corpus_expected.json), so it reports what steady-py's current analysis finds in the
fixtures; a reason --coverage doesn't reflect points at a harvesting gap. It also lists the values
of steady-py's own vocabularies that no fixture exhibits (needs steady-py importable, as in the dev
venv). To cover something new: check
--coverage, add a notebook with its reason to NOTEBOOKS, rerun, and regenerate the golden file
(working_rules.md, section 6).

Stdlib only, apart from --coverage's vocabulary gaps. Paths are relative to the repository, wherever
the script is run from; --source is relative to the current directory.
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

REPO = Path(__file__).resolve().parents[1]
DEST = REPO / "tests" / "fixtures" / "corpus"
GOLDEN = REPO / "tests" / "fixtures" / "corpus_expected.json"
CHANGES = "None."  # changes beyond stripping, by file; written into ATTRIBUTION.md

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

# (path under the corpus, why it is a fixture). For coverage picks, the reason names what the notebook
# was the first to cover when chosen; it may cover more.
NOTEBOOKS: List[Tuple[str, str]] = [
    # Install-line coverage.
    ("packt/Data-Strategy-for-LLMs/chapter_03/Jupyter_Notebooks/Chapter_3_Notebook.ipynb",
     "installs inside def, try/except, if-Colab and else blocks; subprocess list installs; --user, "
     "--break-system-packages, --force-reinstall; variable and range specs; nltk downloads; imports before "
     "and after their install"),
    ("huggingface/examples/onnx-export.ipynb",
     "sys.executable installs; -f and -i; environment variables in lines; git and URL specs"),
    ("teachopencadd/teachopencadd/talktorials/T019_md_simulation/talktorial.ipynb",
     "the conda magic; -r and --no-deps; installs under if; CLI-only installs"),
    ("datashader/8_Polygons.ipynb", "the holoviews extension and parquet signals; an undeclared dependency"),
    ("huggingface/lerobot/training-act.ipynb", "an editable (-e) install; chained shell commands; no requirements"),
    ("sagemaker/sm-async_inference_with_python_sdk.ipynb", "python -m pip; upgrading pip itself"),
    ("huggingface/diffusers/geodiff_molecule_conformation.ipynb", "conda in a shell line; local-path specs"),
    ("cookbook/quickstarts/Get_started_LyriaRealTime.ipynb", "a trailing comment on an install line; an unparsable spec"),
    ("sagemaker/sm-triton_inferentia2.ipynb", "--extra-index-url; extras"),
    ("handson-ml3/tools_pandas.ipynb", "the Excel signal"),
    ("huggingface/longform-qa/Long_Form_Question_Answering_with_ELI5_and_Wikipedia.ipynb", "the Styler signal"),
    ("kaggle_samples/failed/spooky-author-identification_jaymanvirk_fine-tuning-logreg-rf-nb-svm-and-xgboost_v7_C1.ipynb",
     "a spaCy model download"),
    ("huggingface/course/en/chapter13/grpo_finetune.ipynb", "--no-build-isolation; --progress-bar"),
    ("cookbook/examples/langchain/Chat_with_SQL_using_langchain.ipynb", "the SQL signal"),
    ("huggingface/examples/timesfm2_5.ipynb", "a declared dependency"),
    ("huggingface/diffusers/sd_textual_inversion_training.ipynb", "an install under elif; --pre"),
    ("huggingface/diffusers/clip_guided_img2img_stable_diffusion.ipynb",
     "an install line of an uncommon form, imported after the install"),
    ("packt/Data-Strategy-for-LLMs/chapter_05/Jupyter_Notebooks/Chapter_5_Notebook.ipynb", "an install inside a for loop"),
    ("sagemaker/sm-pipelines_step_decorator_with_classic_training_step.ipynb", "a requirements file"),
    ("huggingface/diffusers_doc/en/pytorch/weighted_prompts.ipynb", "uv"),
    ("huggingface/peft/gemma_7b_english_quotes.ipynb", "pip3"),
    ("huggingface/smolagents_doc/en/inspect_runs.ipynb", "a %pip line imported before the install"),
    # Chosen by hand.
    ("mine/medgemma-kaggle.ipynb", "the author's own case, built around real problems"),
    ("mine/silhouette-testing.ipynb", "the author's own case, built around real problems"),
    ("kaggle_samples/failed/plant-pathology-2020-fgvc7_mamamot_fastai-v2-example_v2_C1.ipynb",
     "a long real competition notebook with 9 install lines"),
    ("ISLP_labs/Ch06-varselect-lab.ipynb", "a course notebook, steady-py's core audience"),
    # Analysis coverage.
    ("kaggle_corpus/310110700.ipynb",
     "a computed --target install for another environment (other_environment_install); --no-index, "
     "--find-links, --ignore-installed"),
    ("kaggle_corpus/130818535.ipynb",
     "conda into a named environment; a requirements file; an unknown option; external_requirement; "
     "the conda notice"),
    ("kaggle_corpus/246556058.ipynb", "platform pseudo-modules; a build tool; --force-reinstall, --index-url, --no-cache-dir"),
    ("kaggle_corpus/62497958.ipynb", "TensorFlow GPU detection; an install under except; a /kaggle/input wheel (path target)"),
    ("kaggle_corpus/295962447.ipynb", "JAX GPU detection; extras; unparseable_cell"),
    ("kaggle_corpus/209669249.ipynb", "%%writefile script dependencies; computed_install"),
    ("kaggle_corpus/282973518.ipynb", "--config-settings; --no-binary"),
]
PATHS = [path for path, _ in NOTEBOOKS]

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
    lines += ["## Changes beyond stripping", "", CHANGES, ""]
    return "\n".join(lines)


def readme() -> str:
    """tests/fixtures/corpus/README.md: what these notebooks are, and where everything about them lives."""
    return "\n".join([
        "# Corpus fixtures",
        "",
        "Generated by tools/corpus_fixtures.py; don't edit by hand.",
        "",
        "Real notebooks from steady-py's test corpus, chosen so that together they exhibit every install-line form",
        "and analysis signal found in the corpus. They are the input to tests/characterization/test_corpus.py, which",
        "compares snapshot's analysis of them with a golden file.",
        "",
        "1. Why each notebook is here: the NOTEBOOKS list in tools/corpus_fixtures.py, one reason per entry,",
        "   taken from the scans that selected it.",
        "2. What they cover now, and what no fixture covers: `python tools/corpus_fixtures.py --coverage`.",
        "3. What the test expects: tests/fixtures/corpus_expected.json. How to regenerate it: docs/working_rules.md,",
        "   section 6.",
        "4. Sources, licenses and changes: ATTRIBUTION.md in this directory.",
        "5. To add, remove or change a fixture: edit NOTEBOOKS, rerun the script, and regenerate the golden file.",
        "   Don't edit the notebooks here directly.",
        "",
    ])


def write_docs() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    (DEST / "ATTRIBUTION.md").write_text(attribution(PATHS), encoding="utf-8")
    (DEST / "README.md").write_text(readme(), encoding="utf-8")


def _signals(golden: Dict[str, Dict[str, List[str]]]) -> Dict[str, Dict[str, Set[str]]]:
    """Signal kind -> value -> fixtures exhibiting it, from the golden file's canonical form."""
    found: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    for path, fields in golden.items():
        for kind in ("warnings", "notices"):
            for item in fields.get(kind, []):
                found[kind[:-1]][json.loads(item)[0]].add(path)
        for item in fields.get("dependencies", []):
            dep = json.loads(item)
            found["dependency status"][dep["status"]].add(path)
            found["dependency source"][dep["source"]].add(path)
        for item in fields.get("platform_pseudo_modules", []):
            found["platform module"][json.loads(item)].add(path)
        for item in fields.get("install_lines", []):
            line = json.loads(item)
            found["install tool"][line["tool"]].add(path)
            found["install invocation"][line["invocation"]].add(path)
            found["install guard"][(line["guard"] or {}).get("kind", "none")].add(path)
            found["install readability"][line["readability"]].add(path)
            if not line["kernel_target"]:
                found["install target"]["not the kernel's environment"].add(path)
            for target in line["targets"]:
                found["install target kind"][target["kind"]].add(path)
            for option in line["options"]:
                found["install option"][option["name"] if option.get("known", True) else "(unknown option)"].add(path)
    return found


def _vocabularies() -> Dict[str, Set[str]]:
    """steady-py's own value sets for the signal kinds it defines as constants; empty when it isn't importable."""
    sys.path.insert(0, str(REPO / "src"))
    try:
        from steady_py import constants as c
    except ImportError:
        return {}
    def values(cls: type) -> Set[str]:
        return {v for k, v in vars(cls).items() if k.isupper() and isinstance(v, str)}
    return {
        "dependency status": values(c.DependencyStatus) - {c.DependencyStatus.NOT_INSTALLED},  # manifest only
        "install tool": values(c.Tool),
        "install invocation": values(c.Invocation),
        "install guard": values(c.GuardKind) | {"none"},
        "install readability": values(c.Readability),
        "install target kind": values(c.TargetKind),
    }


def coverage() -> int:
    if not GOLDEN.is_file():
        print(f"No golden file at {GOLDEN}; coverage is read from it.", file=sys.stderr)
        return 1
    found = _signals(json.loads(GOLDEN.read_text(encoding="utf-8")))
    vocab = _vocabularies()
    for kind in sorted(found.keys() | vocab.keys()):
        print(f"{kind}:")
        for value in sorted(found.get(kind, {})):
            paths = sorted(found[kind][value])
            more = f" (+{len(paths) - 3} more)" if len(paths) > 3 else ""
            print(f"  {value}: {', '.join(paths[:3])}{more}")
        for value in sorted(vocab.get(kind, set()) - set(found.get(kind, {}))):
            print(f"  {value}: NOT COVERED")
    if not vocab:
        print("(steady-py isn't importable, so values no fixture exhibits aren't listed.)")
    else:
        print("Warning, notice, source, platform-module and option values have no list in steady-py's constants, "
              "so only what is covered is shown for them.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=str(REPO / "test_notebooks"),
                        help="the corpus directory (default: test_notebooks in the repository)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--docs", action="store_true", help="rewrite README.md and ATTRIBUTION.md only")
    mode.add_argument("--coverage", action="store_true", help="what the fixtures exhibit, and the gaps")
    args = parser.parse_args()
    if args.coverage:
        return coverage()
    if args.docs:
        write_docs()
        print(f"Wrote README.md and ATTRIBUTION.md to {DEST}.")
        return 0
    source = Path(args.source)

    missing = [p for p in PATHS if not (source / p).is_file()]
    if missing:
        print(f"Not found under {source}:", *(f"  {p}" for p in missing), sep="\n", file=sys.stderr)
        return 1

    for stale in DEST.rglob("*.ipynb"):
        if stale.relative_to(DEST).as_posix() not in PATHS:
            stale.unlink()
    flagged = 0
    total = 0
    for rel in PATHS:
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
    write_docs()

    print(f"Wrote {len(PATHS)} notebooks ({total / 1024:.0f} KiB), README.md and ATTRIBUTION.md to {DEST}.")
    if flagged:
        print(f"{flagged} possible secret(s) above: review them before committing.")
        return 1
    print("No likely secrets found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
