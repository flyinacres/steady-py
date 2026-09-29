"""Corpus scan for the install-line and declarations specs.

1. Import/install join   2. Guards and platform checks
3. Optional-dependency signals   4. Install and conda forms
Usage: python corpus_install_scan.py <corpus_dir> [out.csv] [--min-python X.Y]
Stdlib only; independent of steady-py's harvester. Install lines are parsed as the shell and pip
would: a trailing ' #' comment is dropped and combined short flags (-qr file) are expanded; both are
counted (trailing_comment_lines, combined_short_flag_lines) and flagged in the CSV detail.
Mentions of 'pip install' in comments or ordinary strings (print, variables) are skipped and counted.
Notebooks whose code (comments ignored) duplicates an earlier one are skipped and counted.
--min-python skips notebooks whose recorded Python is older (e.g. 3.8); unknown versions are kept.
Notebooks in other languages (R, Julia) are skipped and counted as skipped_language:<name>.
The CSV has a python column (major.minor, or empty) for cross-tabs by era. The console summary is
also written to <out>_summary.txt next to the CSV.

Rearchitecture sizing (counts in the summary, up to five example cells per category at its end):
5. Magic forms (magic_form:*): assignments from shell or magic (x = !cmd, x = %cmd), help (obj?),
   indented magics, lines starting with a % or != operator (continuations the blanking breaks),
   and cell magics by name (cell_magic:*). Rare forms also get CSV rows (kind "magic").
6. Parse outcome per Python cell (parse:*): as written, after blanking %/! lines the way steady-py
   does today, and, when IPython is importable, after IPython's own transform. Cells that fail
   either way get CSV rows (kind "parse"); *_with_import counts those that hold an import.
7. Shell-joined install lines (shell_joined:and/or/seq): '&&', '||' or ';' before 'pip install'.
   '&&' now counts as guarded (enclosing:shell_cond). Installs inside a %%bash if/for/while/case
   block count as enclosing:shell_block, and installs anywhere inside a function as in_function.
Progress: a dot per 100 notebooks, a count per 1000.
"""
import ast, csv, hashlib, itertools, json, re, shlex, sys, warnings
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
SHELL_GUARD = re.compile(r"\[\s|\[\[|\btest\s|\bif\s|\|\||&&")
CONDA = re.compile(r"(?:^|[!%\s'\"])(conda|mamba|micromamba)\s+(install|create|env\s+(?:create|update))\b")

NAME = r"[A-Za-z_][\w.]*"
MAGIC_FORMS = {  # form: (pattern, gets CSV rows)
    "assign_shell": (re.compile(rf"^\s*{NAME}(?:\s*,\s*{NAME})*\s*=\s*!(?!=)"), True),
    "assign_magic": (re.compile(rf"^\s*{NAME}(?:\s*,\s*{NAME})*\s*=\s*%\w"), True),
    "help": (re.compile(rf"^\s*(?:\?{{1,2}}{NAME}|{NAME}(?:\(\))?\?{{1,2}})\s*$"), True),
    "indented_line_magic": (re.compile(r"^\s+%\w"), True),
    "indented_shell": (re.compile(r"^\s+!(?!=)"), True),
    "percent_operator_line": (re.compile(r"^\s*%(?=[\s(\[{'\"])"), True),
    "not_equal_line": (re.compile(r"^\s*!="), True),
    "line_magic": (re.compile(r"^%\w"), False),
    "shell_line": (re.compile(r"^!(?!=)"), False),
}
SHELL_CELL = re.compile(r"^%%(?:bash|sh|script\s+(?:ba)?sh)\b")
SHELL_OPEN = re.compile(r"^\s*(?:if|for|while|until|case)\b")
SHELL_CLOSE = re.compile(r"^\s*(?:fi|done|esac)\b")
EXAMPLES = defaultdict(list)  # category: up to five "notebook:cell" strings
try:
    from IPython.core.inputtransformer2 import TransformerManager
    IPY = TransformerManager()
except ImportError:
    IPY = None

def example(category, nb, ci):
    if len(EXAMPLES[category]) < 5: EXAMPLES[category].append(f"{nb}:{ci}")

def parses(src):
    """None when src parses, else the SyntaxError message and line."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ast.parse(src)
        return None
    except SyntaxError as e: return f"{e.msg} (line {e.lineno})"
    except Exception as e: return f"{type(e).__name__}: {e}"

def parse_outcome(src):
    """(raw, blanked, ipython) error messages, each None when that version parses; ipython is
    'n/a' without IPython. Blanking mirrors steady-py: lines starting with % or ! become empty."""
    blanked = "\n".join("" if l.strip().startswith(("%", "!")) else l for l in src.splitlines())
    ipy = "n/a"
    if IPY is not None:
        try: ipy = parses(IPY.transform_cell(src))
        except Exception as e: ipy = f"transform {type(e).__name__}: {e}"
    return parses(src), parses(blanked), ipy

def ancestors(lines, i):
    """Every block header enclosing line i, innermost first (see enclosing)."""
    ind = len(lines[i]) - len(lines[i].lstrip())
    for j in range(i - 1, -1, -1):
        if ind == 0: return
        l = lines[j]
        if not l.strip() or l.lstrip().startswith("#"): continue
        lind = len(l) - len(l.lstrip())
        if lind < ind:
            if l.split("#")[0].rstrip().endswith(":"): yield l.strip()
            ind = lind

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
    if re.match(r"pip3?\s", s): return "bare_pip"  # IPython automagic
    if s.startswith("%pip"): return "pip_magic"
    if "{sys.executable}" in s: return "sys_executable"
    if s.startswith("%"): return "magic"
    if re.search(r"-m\s+pip", s): return "python_m_pip"
    if re.search(r"os\.system|subprocess|get_ipython", s): return "call"
    if s.startswith("!"): return "shell"
    return "other"

def split_comment(text):
    """(code, had_comment): cut at a '#' that starts a word outside quotes, as the shell does.
    A '#' inside a word (url#egg=x) is kept."""
    quote = None
    for i, ch in enumerate(text):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1].isspace()):
            return text[:i], True
    return text, False

def mention_kind(prefix, form):
    """Why a 'pip install' match is only a mention: inside a comment, or inside a string that is
    not passed to a shell-running call. None when it is a real install."""
    if split_comment(prefix)[1]: return "comment_mention"
    in_string = sum(prefix.count(q) for q in "'\"") % 2 == 1
    return "string_mention" if in_string and form not in ("call", "sys_executable") else None

def parse_tokens(toks):
    """(flags, reqs, combined): combined is True when a value flag sat inside a short-flag cluster."""
    flags, reqs, skip, combined = set(), [], False, False
    for t in toks:
        if t in STOP or t.startswith((">", "2>")): break
        t = t.strip("'\"),;]")
        if not t or t == "@": continue
        if skip: skip = False; continue
        if t.startswith("--"):
            name = t.split("=", 1)[0]; flags.add(name)
            skip = name in VALUE_FLAGS and "=" not in t
        elif t.startswith("-") and len(t) > 1:
            for k, c in enumerate(t[1:], 1):  # -qr file, -rfile: optparse reads the rest as the value
                flags.add("-" + c)
                if "-" + c in VALUE_FLAGS:
                    combined |= k > 1
                    skip = k == len(t) - 1
                    break
        else: reqs.append(req_info(t))
    return flags, reqs, combined

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

def language(meta):
    """The notebook's language, lowercased; python when nothing is recorded."""
    info, spec = meta.get("language_info") or {}, meta.get("kernelspec") or {}
    return str(info.get("name") or spec.get("language") or "python").lower()

def python_version(meta):
    """Major.minor of the notebook's recorded Python, or None (also for other languages)."""
    if language(meta) != "python": return None
    version = (meta.get("language_info") or {}).get("version") or ""
    if not version and "python2" in str((meta.get("kernelspec") or {}).get("name", "")): return "2"
    m = re.match(r"(\d+)\.(\d+)", version)
    return f"{m.group(1)}.{m.group(2)}" if m else None

def scan_notebook(nb, cells, rows, meta=None):
    stats = Counter()
    stats[f"nb_python:{python_version(meta or {}) or 'unknown'}"] += 1
    imports, installs, shell_cmds, text = {}, [], set(), []
    for ci, cell in enumerate(cells, 1):
        if cell.get("cell_type") != "code": continue
        src = cell.get("source", "")
        lines = re.sub(r"\\\n", " ", "".join(src) if isinstance(src, list) else src).splitlines()
        body = "\n".join(lines)
        raw_src = "".join(src) if isinstance(src, list) else src
        first = raw_src.lstrip().split("\n", 1)[0].strip()
        if first.startswith("%%"):
            stats[f"cell_magic:{first[2:].split()[0] if first[2:].split() else ''}"] += 1
        else:
            outcome = dict(zip(("raw", "blanked", "ipython"), parse_outcome(raw_src)))
            has_import = bool(re.search(r"^\s*(?:import|from)\s+[\w.]+", raw_src, re.M))
            stats["parse:cells"] += 1
            for how, err in outcome.items():
                if err and err != "n/a":
                    stats[f"parse:{how}_fail"] += 1
                    stats[f"parse:{how}_fail_with_import"] += has_import
            if outcome["blanked"] and outcome["ipython"] is None:
                stats["parse:fixed_by_ipython"] += 1; example("parse:fixed_by_ipython", nb, ci)
            if outcome["ipython"] not in (None, "n/a") and not outcome["blanked"]:
                stats["parse:broken_by_ipython"] += 1; example("parse:broken_by_ipython", nb, ci)
            if outcome["blanked"] or outcome["ipython"] not in (None, "n/a"):
                status = "/".join(f"{how}_{'ok' if not err else 'n/a' if err == 'n/a' else 'fail'}" for how, err in outcome.items())
                err = outcome["blanked"] or outcome["ipython"]
                rows.append(["parse", nb, ci, "cell", err[:200], status, first[:200]])
        shell_cell, depth, depths = bool(SHELL_CELL.match(first)), 0, []
        for l in lines:
            if shell_cell and SHELL_CLOSE.match(l): depth = max(depth - 1, 0)
            depths.append(depth)
            if shell_cell and SHELL_OPEN.match(l): depth += 1
        python_body = not first.startswith("%%") or first[2:].split()[:1] in (["time"], ["timeit"], ["capture"])
        for li, l in enumerate(lines if python_body else []):
            if l.lstrip().startswith("#"): continue
            for form, (pat, row) in MAGIC_FORMS.items():
                if pat.match(l):
                    stats[f"magic_form:{form}"] += 1; example(f"magic_form:{form}", nb, ci)
                    if row: rows.append(["magic", nb, ci, form, "", "", l.strip()[:200]])
        for m in PIP_LIST.finditer(body):  # list-form subprocess calls, matched across lines
            li = body.count("\n", 0, m.start())
            flags, reqs, _ = parse_tokens(re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)))
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
                form, prefix = form_of(s), raw[:m.start()]
                if (why := mention_kind(prefix, form)):
                    stats[f"skipped:{why}"] += 1
                    continue
                args, commented = split_comment(m.group(1))
                try: toks = shlex.split(args)
                except ValueError: toks = args.split()
                flags, reqs, combined = parse_tokens(toks)
                enc = enclosing(lines, li)
                if depths[li]: enc = "shell_block " + first
                shell_part = re.split(r"[!'\"]", prefix)[-1]
                for op, name in (("&&", "and"), ("||", "or"), (";", "seq")):
                    if op in shell_part: stats[f"shell_joined:{name}"] += 1; example(f"shell_joined:{name}", nb, ci)
                if SHELL_GUARD.search(prefix): enc = "shell_cond " + prefix.strip()
                if any(a.startswith(("def ", "async def ")) for a in ancestors(lines, li)):
                    stats["in_function"] += 1; example("in_function", nb, ci)
                installs.append(dict(pos=(ci, li), form=form, flags=flags, reqs=reqs, enc=enc, line=s,
                                     notes=[n for n, on in (("#comment", commented), ("combined_flags", combined)) if on]))
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
        notes = ins.get("notes", [])
        stats["trailing_comment_lines"] += "#comment" in notes
        stats["combined_short_flag_lines"] += "combined_flags" in notes
        detail = f"{' '.join(sorted(ins['flags']))} | enc={head}{'/' + '+'.join(platform) if platform else ''}"
        detail += "".join(f" | {n}" for n in notes)
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
    "not_imp": ["req_join:not_imported"], "guarded": ["enclosing:shell_cond", "enclosing:shell_block", "enclosing:if", "enclosing:elif", "enclosing:else", "enclosing:try", "enclosing:except"],
    "colab": ["nb_platform:colab"], "odd_form": ["form:other", "form:sys_executable", "form:subprocess_list", "form:python_m_pip", "form:uv_magic", "form:magic", "form:call"],
    "conda": ["conda:"], "undecl": ["nb_signal_undeclared:"],
}

def group_value(c, keys):
    return sum(v for k, v in c.items() for key in keys if (k == key or (key.endswith(":") and k.startswith(key))))

def older(version, minimum):
    if not minimum or version is None: return False
    as_tuple = lambda v: tuple(int(p) for p in v.split("."))
    return as_tuple(version) < as_tuple(minimum)

def main(root, out_csv, min_python=None):
    root = Path(root)
    total, groups, rows, hashes = Counter(), defaultdict(Counter), [], set()
    print(f"IPython transform: {'on' if IPY else 'off (IPython not importable; parse:ipython_* not counted)'}")
    for n, nb in enumerate(sorted(root.rglob("*.ipynb")), 1):
        if n % 100 == 0: print("." if n % 1000 else f" {n}", end="" if n % 1000 else "\n", flush=True)
        if ".ipynb_checkpoints" in nb.parts: continue
        rel = nb.relative_to(root).parts
        group = rel[0] if len(rel) > 1 else "(root)"
        try:
            data = json.loads(nb.read_text(encoding="utf-8"))
            cells, meta = data.get("cells", []), data.get("metadata", {})
            h = code_hash(cells)
            py = python_version(meta)
            if h in hashes: c = Counter(duplicate_skipped=1)
            elif language(meta) != "python": c = Counter({f"skipped_language:{language(meta)}": 1})
            elif older(py, min_python): c = Counter(skipped_old_python=1)
            else:
                hashes.add(h); nb_rows = []
                c = scan_notebook(nb, cells, nb_rows, meta)
                rows += [[group, py or ""] + r for r in nb_rows]
        except Exception as e: c = Counter(unreadable=1); print(f"skip {nb}: {e}")
        total.update(c); groups[group].update(c)
    summary = [f"{k:40} {total[k]}" for k in sorted(total)] + [""]
    summary.append(f"{'group':28}" + "".join(f"{col:>10}" for col in GROUP_COLS))
    for g in sorted(groups):
        summary.append(f"{g[:28]:28}" + "".join(f"{group_value(groups[g], keys):>10}" for keys in GROUP_COLS.values()))
    summary += ["", "examples (notebook:cell):"] + [f"{k}\n    " + "\n    ".join(v) for k, v in sorted(EXAMPLES.items())]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["group", "python", "kind", "notebook", "cell", "form_or_signal", "detail", "status", "line"])
        w.writerows(rows)
    summary_path = Path(out_csv).with_name(Path(out_csv).stem + "_summary.txt")
    args = " ".join(sys.argv[1:])
    summary_path.write_text(f"corpus_install_scan.py {args}\n\n" + "\n".join(summary) + "\n", encoding="utf-8")
    print("\n" + "\n".join(summary))
    print(f"detail rows: {out_csv}\nsummary: {summary_path}")

if __name__ == "__main__":
    args = sys.argv[1:]
    minimum = None
    if "--min-python" in args:
        i = args.index("--min-python"); minimum = args[i + 1]; del args[i:i + 2]
    main(args[0], args[1] if len(args) > 1 else "install_lines.csv", minimum)
