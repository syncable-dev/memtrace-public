"""
Extract ground-truth (symbol, file) pairs from mempalace using Python's
built-in `ast` module — zero dependency on any code-intelligence tool.

Every competing system is later scored against this corpus.  Since the
corpus is extracted by a standard Python AST parser that ships with
CPython, no tool's parser has a "home-field advantage" in the ground
truth itself.  Coverage differences between tools (did they even index
this symbol?) become a *reportable metric*, not a hidden bias.
"""
import ast
import json
import os
import random
import sys
from pathlib import Path

REPO_ROOT     = Path("/Users/alexthh/Desktop/ZeroToDemo/mempalace")
REPO_PARENT   = REPO_ROOT.parent          # so paths are "mempalace/..."
OUT_CORPUS    = Path(__file__).parent / "corpus.json"
OUT_DATASET   = Path(__file__).parent / "dataset.json"
N_QUERIES     = 1000
IGNORE_NAMES  = {"__init__", "__repr__", "__str__", "__eq__", "__hash__",
                 "__call__", "__enter__", "__exit__", "__len__", "__iter__",
                 "main", "test", "setup", "teardown",
                 "get", "set", "run", "_", "__",}
SKIP_DIRS     = {".git", "__pycache__", ".venv", "node_modules", "dist",
                 "build", ".pytest_cache", ".mypy_cache", "target"}


def extract_symbols(source_path: Path):
    """Return list of (name, kind, line) for every top-level / nested
    FunctionDef | AsyncFunctionDef | ClassDef that has a name long enough to
    be queryable."""
    try:
        src = source_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    try:
        tree = ast.parse(src, filename=str(source_path))
    except SyntaxError:
        return []

    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "Method" if _is_method(node, tree) else "Function"
        elif isinstance(node, ast.ClassDef):
            kind = "Class"
        else:
            continue
        name = node.name
        if name in IGNORE_NAMES or len(name) <= 3 or name.startswith("_"):
            continue
        found.append({"name": name, "kind": kind, "line": node.lineno})
    return found


def _is_method(fn_node, tree):
    # Crude but accurate enough: a function inside any ClassDef body is a Method.
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for child in ast.walk(cls):
                if child is fn_node:
                    return True
    return False


def scan_repo():
    corpus = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = Path(dirpath) / fname
            relpath = fpath.relative_to(REPO_PARENT)  # "mempalace/..."
            for sym in extract_symbols(fpath):
                corpus.append({
                    "name":      sym["name"],
                    "kind":      sym["kind"],
                    "line":      sym["line"],
                    "file_path": str(relpath),
                })
    return corpus


def main():
    print(f"Scanning {REPO_ROOT}...")
    corpus = scan_repo()
    print(f"Extracted {len(corpus)} symbol occurrences from {REPO_ROOT}")

    # Unique-by-(name, file) so we don't sample the same symbol twice.
    seen = set()
    uniq = []
    for sym in corpus:
        key = (sym["name"], sym["file_path"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(sym)
    print(f"Unique (symbol, file) pairs: {len(uniq)}")

    with open(OUT_CORPUS, "w") as f:
        json.dump(corpus, f, indent=2)

    random.seed(1337)
    random.shuffle(uniq)
    sample = uniq[: N_QUERIES]

    dataset = []
    for i, s in enumerate(sample):
        dataset.append({
            "id":            f"q{i+1}",
            "query":         s["name"],               # exact symbol name
            "expected_file": s["file_path"],          # "mempalace/..."
            "target_symbol": s["name"],
            "kind":          s["kind"],
        })
    with open(OUT_DATASET, "w") as f:
        json.dump(dataset, f, indent=2)
    print(f"Wrote {len(dataset)} queries to {OUT_DATASET}")
    print(f"Wrote full {len(corpus)} corpus entries to {OUT_CORPUS}")


if __name__ == "__main__":
    sys.exit(main())
