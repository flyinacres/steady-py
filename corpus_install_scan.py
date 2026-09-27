"""Count notebook install lines by how they could affect a pin.

Usage: python corpus_install_scan.py <corpus_dir> [out.csv]
Independent of steady-py's harvester on purpose (that harvester drops flags).
"""
import csv, json, re, shlex, sys
from collections import Counter
from pathlib import Path

PIP_RE = re.compile(r"\bpip3?\s+install\b(.*)", re.I)
VALUE_FLAGS = {"-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-i", "--index-url",
               "--extra-index-url", "-f", "--find-links", "-t", "--target", "--prefix", "--root"}

def form_of(s):
    if s.startswith("%uv"): return "uv_magic"
    if s.startswith("%pip"): return "pip_magic"
    if re.search(r"-m\s+pip", s): return "python_m_pip"
    if re.search(r"os\.system|subprocess|get_ipython", s): return "call"
    if s.startswith("!"): return "shell"
    return "bare"

def classify(tail):
    try: toks = shlex.split(tail)
    except ValueError: toks = tail.split()
    flags, reqs, skip = set(), [], False
    for t in toks:
        t = t.strip("'\"),;")
        if skip: skip = False; continue
        if t.startswith("-"):
            name = t.split("=", 1)[0]
            flags.add(name)
            skip = name in VALUE_FLAGS and "=" not in t
            continue
        if not t: continue
        if "$" in t or "{" in t: kind = "variable"
        elif "://" in t or "git+" in t or " @ " in t or t.startswith("@"): kind = "url"
        elif "==" in t: kind = "exact"
        elif re.search(r"[<>~!]=?|>=", t): kind = "range"
        else: kind = "bare"
        m = re.match(r"[A-Za-z0-9._-]+", t)
        reqs.append((re.sub(r"[-_.]+", "-", m.group(0)).lower() if m else t, kind))
    upgrade = bool(flags & {"-U", "--upgrade"})
    force = "--force-reinstall" in flags
    return flags, reqs, upgrade, force

def main(root, out_csv):
    stats, rows = Counter(), []
    for nb in Path(root).rglob("*.ipynb"):
        if ".ipynb_checkpoints" in nb.parts: continue
        try: cells = json.loads(nb.read_text(encoding="utf-8")).get("cells", [])
        except Exception: stats["unreadable"] += 1; continue
        stats["notebooks"] += 1
        seen, moving, has_install = Counter(), False, False
        for ci, cell in enumerate(cells, 1):
            if cell.get("cell_type") != "code": continue
            src = cell.get("source", "")
            src = "".join(src) if isinstance(src, list) else src
            for raw in re.sub(r"\\\n", " ", src).splitlines():
                m = PIP_RE.search(raw)
                if not m or raw.lstrip().startswith("#"): continue
                has_install = True
                s = raw.strip()
                flags, reqs, upgrade, force = classify(m.group(1))
                guarded = raw[:1] in " \t"
                stats["lines"] += 1
                stats[f"form:{form_of(s)}"] += 1
                stats["guarded_lines"] += guarded
                stats["upgrade_lines"] += upgrade
                stats["force_lines"] += force
                stats["requirement_file_lines"] += bool(flags & {"-r", "--requirement"})
                for name, kind in reqs:
                    stats[f"req:{kind}"] += 1
                    seen[name] += 1
                if upgrade or force or any(k == "exact" for _, k in reqs): moving = True
                rows.append([str(nb), ci, form_of(s), guarded, upgrade, force,
                             " ".join(sorted(flags)), " ".join(f"{n}:{k}" for n, k in reqs), s[:200]])
        stats["nb_with_install"] += has_install
        stats["nb_with_pin_moving_line"] += moving
        stats["nb_with_repeat_install"] += any(v > 1 for v in seen.values())
    for k in sorted(stats): print(f"{k:28} {stats[k]}")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["notebook", "cell", "form", "guarded", "upgrade", "force", "flags", "reqs", "line"])
        w.writerows(rows)
    print(f"line detail: {out_csv}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "install_lines.csv")
