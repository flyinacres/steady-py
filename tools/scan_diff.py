"""Compare two `steady-py scan --format json` runs per notebook, and copy a fixed corpus sample.

  python tools/scan_diff.py diff OLD.json NEW.json
  python tools/scan_diff.py sample SRC_DIR DST_DIR --every 10

Stdlib only, so it runs in any venv. Comment text is not compared (display output, K10).
"""
import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

LIST_FIELDS = ("local_modules", "build_and_packaging_tools", "platform_pseudo_modules", "promotions", "install_lines")


def _norm(path: str, root: str) -> str:
    path, root = path.replace("\\", "/"), root.replace("\\", "/").rstrip("/") + "/"
    return path[len(root):] if path.startswith(root) else path


def _canon(item: Any) -> str:
    return json.dumps(item, sort_keys=True)


def _diagnostic(event: Dict[str, Any]) -> str:
    return _canon([event["type"], event["detail"], event.get("cell_idx"), event.get("line_idx")])


def _items(nb: Dict[str, Any]) -> Dict[str, Counter[str]]:
    """Each field as a multiset, so repeated identical items are counted, not collapsed."""
    out: Dict[str, Counter[str]] = {
        "dependencies": Counter(_canon({k: v for k, v in d.items() if k != "comment"}) for d in nb.get("dependencies", [])),
        "warnings": Counter(_diagnostic(w) for w in nb.get("warnings", [])),
        "notices": Counter(_diagnostic(n) for n in nb.get("notices", [])),
        "parse_error": Counter([_canon(nb["parse_error"])] if nb.get("parse_error") else []),
    }
    for field in LIST_FIELDS:
        out[field] = Counter(_canon(x) for x in nb.get(field) or [])
    return out


def _load(path: str) -> Dict[str, Dict[str, Counter[str]]]:
    with open(path, encoding="utf-8") as fh:
        run = json.load(fh)
    root = run.get("target_dir") or ""
    notebooks = {_norm(nb["notebook_path"], root): _items(nb) for nb in run["notebooks"]}
    for err in run.get("summary", {}).get("parse_errors", []):
        notebooks.setdefault(_norm(err["path"], root), {})["unreadable"] = Counter([_canon(err["cause"])])
    return notebooks


def diff(old_path: str, new_path: str) -> int:
    old, new = _load(old_path), _load(new_path)
    only_old, only_new = sorted(old.keys() - new.keys()), sorted(new.keys() - old.keys())
    counts: Counter[Tuple[str, str]] = Counter()
    changed: List[Tuple[str, List[str]]] = []
    for i, path in enumerate(sorted(old.keys() & new.keys()), 1):
        if i % 500 == 0:
            print(".", end="", file=sys.stderr, flush=True)
        lines: List[str] = []
        for field in sorted(old[path].keys() | new[path].keys()):
            a, b = old[path].get(field, Counter()), new[path].get(field, Counter())
            for sign, items in (("-", sorted((a - b).elements())), ("+", sorted((b - a).elements()))):
                for item in items:
                    lines.append(f"  {sign} {field}: {item}")
                    counts[(field, sign)] += 1
        if lines:
            changed.append((path, lines))
    print(file=sys.stderr)
    print(f"Compared {len(old.keys() & new.keys())} notebooks; {len(changed)} changed; "
          f"{len(only_old)} only in old; {len(only_new)} only in new.")
    for field in sorted({f for f, _ in counts}):
        print(f"  {field}: -{counts[(field, '-')]} +{counts[(field, '+')]}")
    for label, paths in (("Only in old", only_old), ("Only in new", only_new)):
        if paths:
            print(f"\n{label}:")
            print("\n".join(f"  {p}" for p in paths))
    for path, lines in changed:
        print(f"\n{path}")
        print("\n".join(lines))
    return 0


def sample(src: str, dst: str, every: int) -> int:
    src_dir, dst_dir = Path(src), Path(dst)
    if dst_dir.exists() and any(dst_dir.iterdir()):
        print(f"{dst_dir} is not empty; the sample is copied once.", file=sys.stderr)
        return 1
    paths = sorted(p for p in src_dir.rglob("*.ipynb") if ".ipynb_checkpoints" not in p.parts)
    picked = paths[::every]
    for i, p in enumerate(picked, 1):
        target = dst_dir / p.relative_to(src_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        if i % 100 == 0:
            print(".", end="", file=sys.stderr, flush=True)
    print(f"\nCopied {len(picked)} of {len(paths)} notebooks to {dst_dir}.", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("diff")
    d.add_argument("old")
    d.add_argument("new")
    s = sub.add_parser("sample")
    s.add_argument("src")
    s.add_argument("dst")
    s.add_argument("--every", type=int, default=10)
    args = parser.parse_args()
    if args.command == "diff":
        return diff(args.old, args.new)
    return sample(args.src, args.dst, args.every)


if __name__ == "__main__":
    sys.exit(main())
