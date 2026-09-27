"""Corpus scan for the install-line and declarations specs.

1. Import/install join   2. Guards and platform checks
3. Optional-dependency signals   4. Install and conda forms
Usage: python corpus_install_scan.py <corpus_dir> [out.csv]
Stdlib only; independent of steady-py's harvester.
Notebooks whose code (comments ignored) duplicates an earlier one are skipped and counted.
"""
import csv, hashlib, itertools, json, re, shlex, sys
from collections import Counter, defaultdict
from pathlib import Path

VALUE_FLAGS = {"-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-i", "--index-url",
               "--extra-index-url", "-f", "--find-links", "-t", "--target", "--prefix", "--root"}
STOP = {"&&", "||", ";", "|"}
ALIASES = {"scikit-learn": "sklearn", "scikit-image": "skimage", "opencv-python": "cv2",
           "opencv-python-headless": "cv2", "opencv-contrib-python": "cv2",
           "opencv-contrib-python-headless": "cv2", "pillow": "PIL", "python-dotenv": "dotenv",
           "beautifulsoup4": "bs4", "pyyaml": "yaml", "python-dateutil": "dateutil",
           "protobuf": "google.protobuf", "faiss-cpu": "faiss",
           "faiss-gpu": "faiss", "paddlepaddle": "paddle", "paddlepaddle-gpu": "paddle",
           "pymupdf": "fitz", "tensorflow-gpu": "tensorflow", "tensorflow-cpu": "tensorflow",
           "attrs": "attr", "umap-learn": "umap", "psycopg2-binary": "psycopg2",
           "sagemaker-mlflow": "sagemaker_mlflow", "databricks-sdk": "databricks"}
CLI_NAMES = {"awscli": "aws", "kaggle": "kaggle", "gdown": "gdown"}
PLATFORM = {"colab": r"google\.colab|IN_COLAB|COLAB_|/content\b", "kaggle": r"KAGGLE_KERNEL_RUN_TYPE|KAGGLE_URL_BASE|IN_KAGGLE|['\"]/kaggle",
            "gpu_check": r"cuda\.is_available\(|list_physical_devices\(\s*['\"]GPU",
            "import_error": r"except\s*\(?\s*(?:ImportError|ModuleNotFoundError)"}
SIGNALS = {  # name: (pattern, packages that count as declared)
    "excel": (r"read_excel\(|to_excel\(|ExcelWriter\(", {"openpyxl", "xlrd", "xlsxwriter"}),
    "parquet": (r"read_parquet\(|to_parquet\(", {"pyarrow", "fastparquet"}),
    "gcs_path": (r"(?:read_\w+|to_\w+)\(\s*f?['\"]gs://", {"gcsfs"}),
    "s3_path": (r"(?:read_\w+|to_\w+)\(\s*f?['\"]s3://", {"s3fs"}),
    "hdf": (r"read_hdf\(|to_hdf\(|HDFStore\(", {"tables"}),
    "html_xml": (r"read_html\(|read_xml\(", {"lxml", "html5lib", "beautifulsoup4"}),
    "sql": (r"read_sql(?:_query|_table)?\(|to_sql\(", {"sqlalchemy"}),
    "to_markdown": (r"\.to_markdown\(", {"tabulate"}),
    "styler": (r"\.style\.(?:format\w*|apply\w*|map\w*|highlight_\w+|background_gradient|text_gradient|bar|set_\w+|hide\w*|to_\w+)\(", {"jinja2"}),
    "spacy_model": (r"spacy\.load\(\s*['\"]([^'\"]+)", set()),
    "hv_extension": (r"\b(?:hv|holoviews)\.extension\(", set()),
    "nltk_download": (r"nltk\.download\(", set()),
}
PIP_LIST = re.compile(r"""['"]pip3?['"]\s*,\s*['"]install['"]\s*,([^\]]*)""")  # may span lines
PIP_STR = re.compile(r"\bpip3?\s+install\b((?:(?!&&|\|\||;).)*)")  # stops at shell chaining
SHELL_GUARD = re.compile(r"\[\s|\[\[|\btest\s|\bif\s|\|\|")
CONDA = re.compile(r"(?:^|[!%\s'\"])(conda|mamba|micromamba)\s+(install|create|env\s+(?:create|update))\b")

def canon(n): return re.sub(r"[-_.]+", "-", n).lower()
def import_candidates(pip):
    """Alias, then every mix of '_' and '.' for the hyphens (google-genai -> google.genai, llama-index-llms-x -> llama_index.llms.x)."""
    parts = pip.split("-")
    seps = itertools.product("_.", repeat=min(len(parts) - 1, 6))
    out = [ALIASES[pip]] if pip in ALIASES else []
    for combo in seps:
        out.append(parts[0] + "".join(sep + part for sep, part in zip(combo, parts[1:])))
    return out or [pip]

def first_import(imports, pip):
    hits = [imports[c] for c in import_candidates(pip) if c in imports]
    return min(hits) if hits else None

def form_of(s):
    if s.startswith("%uv"): return "uv_magic"
    if s.startswith("%pip"): return "pip_magic"
    if "{sys.executable}" in s: return "sys_executable"
    if s.startswith("%"): return "magic"
    if re.search(r"-m\s+pip", s): return "python_m_pip"
    if re.search(r"os\.system|subprocess|get_ipython", s): return "call"
    if s.startswith("!"): return "shell"
    return "other"

def parse_tokens(toks):
    flags, reqs, skip = set(), [], False
    for t in toks:
        if t in STOP or t.startswith((">", "2>")): break
        t = t.strip("'\"),;]")
        if not t or t == "@": continue
        if skip: skip = False; continue
        if t.startswith("--"):
            name = t.split("=", 1)[0]; flags.add(name)
            skip = name in VALUE_FLAGS and "=" not in t
        elif t.startswith("-") and len(t) > 1:
            if "-" + t[1] in VALUE_FLAGS: flags.add("-" + t[1]); skip = len(t) == 2
            else: flags.update("-" + c for c in t[1:])
        else: reqs.append(req_info(t))
    return flags, reqs

def req_info(t):
    if "$" in t or "{" in t: return None, "variable"
    if "://" in t or t.startswith("git+"):
        m = re.search(r"egg=([\w.-]+)", t); return (canon(m.group(1)) if m else None), "url"
    if t.startswith((".", "/")) or t.endswith((".whl", ".tar.gz")): return None, "path"
    m = re.match(r"[A-Za-z0-9._-]+", t)
    if not m: return None, "unparsed"
    rest = re.sub(r"^\[[^\]]*\]", "", t[m.end():]).lstrip()
    kind = "url" if rest.startswith("@") else "exact" if rest.startswith("==") else "range" if rest else "bare"
    return canon(m.group(0)), kind

def enclosing(lines, i):
    """Nearest block header (line ending in ':') that encloses line i; skips opening lines of multi-line calls."""
    ind = len(lines[i]) - len(lines[i].lstrip())
    for j in range(i - 1, -1, -1):
        if ind == 0: break
        l = lines[j]
        if not l.strip() or l.lstrip().startswith("#"): continue
        lind = len(l) - len(l.lstrip())
        if lind < ind:
            if l.split("#")[0].rstrip().endswith(":"): return l.strip()
            ind = lind  # continuation parent: look for what encloses it
    return ""

def code_hash(cells):
    code = []
    for c in cells:
        if c.get("cell_type") != "code": continue
        src = c.get("source", "")
        src = "".join(src) if isinstance(src, list) else src
        code += [l.strip() for l in src.splitlines() if l.strip() and not l.strip().startswith("#")]
    return hashlib.sha1("\n".join(code).encode()).hexdigest()

def scan_notebook(nb, cells, rows):
    stats = Counter()
    imports, installs, shell_cmds, text = {}, [], set(), []
    for ci, cell in enumerate(cells, 1):
        if cell.get("cell_type") != "code": continue
        src = cell.get("source", "")
        lines = re.sub(r"\\\n", " ", "".join(src) if isinstance(src, list) else src).splitlines()
        body = "\n".join(lines)
        for m in PIP_LIST.finditer(body):  # list-form subprocess calls, matched across lines
            li = body.count("\n", 0, m.start())
            flags, reqs = parse_tokens(re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)))
            if not reqs and not flags & {"-r", "--requirement"}:
                reqs = [(None, "variable")]  # package passed as a variable, e.g. a loop
            installs.append(dict(pos=(ci, li), form="subprocess_list", flags=flags, reqs=reqs,
                                 enc=enclosing(lines, li), line=" ".join(m.group(0).split())))
        for li, raw in enumerate(lines):
            s = raw.strip()
            if not s or s.startswith("#"): continue
            text.append((ci, raw))
            m = re.match(r"(?:from\s+([\w.]+)\s+import\s+(.+)|import\s+(.+))", s)
            if m:
                if m.group(1):  # from X import a, b -> X, X.a, X.b (a may be a submodule)
                    names = [p.split()[0] for p in m.group(2).strip("()\\ ").split(",") if p.split()]
                    mods = [m.group(1)] + [f"{m.group(1)}.{n}" for n in names if n.isidentifier()]
                else:
                    mods = [p.split()[0] for p in m.group(3).split(",") if p.split()]
                for mod in mods:
                    parts = mod.split(".")
                    if not parts[0].isidentifier(): continue
                    for k in range(1, len(parts) + 1): imports.setdefault(".".join(parts[:k]), (ci, li))
            if s.startswith("!"):
                w = s[1:].split()
                w = w[1:] if w[:1] == ["sudo"] else w
                if w: shell_cmds.add(w[0])
            for m in PIP_STR.finditer(raw):
                try: toks = shlex.split(m.group(1))
                except ValueError: toks = m.group(1).split()
                form, prefix = form_of(s), raw[:m.start()]
                flags, reqs = parse_tokens(toks)
                enc = enclosing(lines, li)
                if SHELL_GUARD.search(prefix): enc = "shell_cond " + prefix.strip()
                installs.append(dict(pos=(ci, li), form=form, flags=flags, reqs=reqs, enc=enc, line=s))
            if (c := CONDA.search(raw)):
                stats[f"conda:{c.group(1)}_{c.group(2).split()[0]}"] += 1
                rows.append(["conda", nb, ci, form_of(s), c.group(0).strip(), "", s[:200]])
    full = "\n".join(r for _, r in text)
    stats["notebooks"] += 1
    stats["nb_condacolab"] += "condacolab" in full
    for k, pat in PLATFORM.items(): stats[f"nb_platform:{k}"] += bool(re.search(pat, full))
    installed, seen = set(), Counter()
    for ins in installs:
        stats["install_lines"] += 1
        stats[f"form:{ins['form']}"] += 1
        head = ins["enc"].split()[0].rstrip(":") if ins["enc"] else "top_level"
        platform = [k for k, p in PLATFORM.items() if re.search(p, ins["enc"])]
        stats[f"enclosing:{head}"] += 1
        for k in platform: stats[f"enclosing_platform:{k}"] += 1
        upgrade = bool(ins["flags"] & {"-U", "--upgrade"})
        stats["upgrade_lines"] += upgrade
        stats["force_reinstall_lines"] += "--force-reinstall" in ins["flags"]
        stats["no_deps_lines"] += "--no-deps" in ins["flags"]
        stats["requirement_file_lines"] += bool(ins["flags"] & {"-r", "--requirement"})
        detail = f"{' '.join(sorted(ins['flags']))} | enc={head}{'/' + '+'.join(platform) if platform else ''}"
        if not ins["reqs"]: rows.append(["install", nb, ins["pos"][0], ins["form"], detail, "no_reqs", ins["line"][:200]])
        for name, kind in ins["reqs"]:
            stats[f"req_kind:{kind}"] += 1
            if name is None: status = "computed" if kind == "variable" else "unnamed"
            elif name == "pip": status = "pip_itself"
            else:
                installed.add(name); seen[name] += 1
                pos = first_import(imports, name)
                if pos: status = "imported_before_install" if pos < ins["pos"] else "imported_after_install"
                elif name in shell_cmds or CLI_NAMES.get(name) in shell_cmds: status = "cli_only"
                else: status = "not_imported"
            stats[f"req_join:{status}"] += 1
            rows.append(["install", nb, ins["pos"][0], ins["form"], f"{name}:{kind} {detail}", status, ins["line"][:200]])
    stats["nb_with_install"] += bool(installs)
    stats["nb_with_repeat_install"] += any(v > 1 for v in seen.values())
    for sig, (pat, pkgs) in SIGNALS.items():
        hit = next(((ci, r, m) for ci, r in text if (m := re.search(pat, r))), None)
        if not hit: continue
        ci, r, m = hit
        if sig == "spacy_model":
            model = m.group(1)
            declared = canon(model) in installed or bool(re.search(rf"spacy\s+download\s+{re.escape(model)}", full))
        else:
            declared = any(p in installed or first_import(imports, p) for p in pkgs) if pkgs else None
        stats[f"nb_signal:{sig}"] += 1
        if declared is False: stats[f"nb_signal_undeclared:{sig}"] += 1
        rows.append(["signal", nb, ci, sig, "", {True: "declared", False: "undeclared", None: "n/a"}[declared], r.strip()[:200]])
    return stats

GROUP_COLS = {  # column: keys summed from per-notebook stats
    "nbs": ["notebooks"], "dups": ["duplicate_skipped"], "w/inst": ["nb_with_install"], "lines": ["install_lines"],
    "-U": ["upgrade_lines"], "==": ["req_kind:exact"], "imp_after": ["req_join:imported_after_install"],
    "not_imp": ["req_join:not_imported"], "guarded": ["enclosing:shell_cond", "enclosing:if", "enclosing:elif", "enclosing:else", "enclosing:try", "enclosing:except"],
    "colab": ["nb_platform:colab"], "odd_form": ["form:other", "form:sys_executable", "form:subprocess_list", "form:python_m_pip", "form:uv_magic", "form:magic", "form:call"],
    "conda": ["conda:"], "undecl": ["nb_signal_undeclared:"],
}

def group_value(c, keys):
    return sum(v for k, v in c.items() for key in keys if (k == key or (key.endswith(":") and k.startswith(key))))

def main(root, out_csv):
    root = Path(root)
    total, groups, rows, hashes = Counter(), defaultdict(Counter), [], set()
    for nb in sorted(root.rglob("*.ipynb")):
        if ".ipynb_checkpoints" in nb.parts: continue
        rel = nb.relative_to(root).parts
        group = rel[0] if len(rel) > 1 else "(root)"
        try:
            cells = json.loads(nb.read_text(encoding="utf-8")).get("cells", [])
            h = code_hash(cells)
            if h in hashes: c = Counter(duplicate_skipped=1)
            else:
                hashes.add(h); nb_rows = []
                c = scan_notebook(nb, cells, nb_rows)
                rows += [[group] + r for r in nb_rows]
        except Exception as e: c = Counter(unreadable=1); print(f"skip {nb}: {e}")
        total.update(c); groups[group].update(c)
    for k in sorted(total): print(f"{k:40} {total[k]}")
    print()
    print(f"{'group':28}" + "".join(f"{col:>10}" for col in GROUP_COLS))
    for g in sorted(groups):
        print(f"{g[:28]:28}" + "".join(f"{group_value(groups[g], keys):>10}" for keys in GROUP_COLS.values()))
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["group", "kind", "notebook", "cell", "form_or_signal", "detail", "status", "line"])
        w.writerows(rows)
    print(f"detail rows: {out_csv}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "install_lines.csv")
