"""Bench #4 edit-script generator.

Walks the scratch_fixture corpus and emits a deterministic sequence of 50
structural edit actions. Each action records:

  - action_type: add_symbol | rename_symbol | move_symbol | delete_symbol
  - the file(s) it touches
  - the target symbol name(s)
  - expected_post_state: what a correct adapter should return for
    find_symbol(name) AFTER the edit has been applied.

Determinism guarantee: the generator uses a seeded RNG and the
corpus's committed contents. Running it twice in a row produces the same
JSON. The resulting `bench_4_edits.json` is committed to the repo so
downstream runners do not re-derive.

This module DOES NOT apply the edits — it only plans them. Applying
and rolling back edits is the Bench #4 runner's job (Phase F tail).
"""
from __future__ import annotations
import ast
import json
import random
from dataclasses import dataclass, asdict
from pathlib import Path


DEFAULT_SEED = 2026_04_21
DEFAULT_NUM_ACTIONS = 50


@dataclass
class Symbol:
    name: str
    kind: str           # "function" | "class" | "method"
    file: str           # relative to corpus root
    line: int


@dataclass
class EditAction:
    id: str
    action_type: str    # add_symbol | rename_symbol | move_symbol | delete_symbol
    file: str
    target_name: str
    # Optional fields per action type:
    new_name: str | None = None
    dest_file: str | None = None
    expected_post_state: dict | None = None


def extract_symbols(corpus_root: Path) -> list[Symbol]:
    """Walk corpus root, parse each .py file, extract top-level + method
    definitions. Test files are included too so edits can target them."""
    symbols: list[Symbol] = []
    for py in sorted(corpus_root.rglob("*.py")):
        if py.name == "__init__.py":
            continue
        rel = py.relative_to(corpus_root).as_posix()
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                symbols.append(Symbol(
                    name=node.name, kind="function", file=rel, line=node.lineno,
                ))
            elif isinstance(node, ast.AsyncFunctionDef):
                symbols.append(Symbol(
                    name=node.name, kind="function", file=rel, line=node.lineno,
                ))
            elif isinstance(node, ast.ClassDef):
                symbols.append(Symbol(
                    name=node.name, kind="class", file=rel, line=node.lineno,
                ))
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(Symbol(
                            name=child.name, kind="method",
                            file=rel, line=child.lineno,
                        ))
    return symbols


def _list_files(corpus_root: Path) -> list[str]:
    files: list[str] = []
    for py in sorted(corpus_root.rglob("*.py")):
        if py.name == "__init__.py":
            continue
        files.append(py.relative_to(corpus_root).as_posix())
    return files


def generate_actions(
    corpus_root: Path,
    num_actions: int = DEFAULT_NUM_ACTIONS,
    seed: int = DEFAULT_SEED,
) -> list[EditAction]:
    """Produce a balanced deterministic edit script.

    Action-type mix: ~25% add, ~30% rename, ~25% move, ~20% delete.
    """
    rng = random.Random(seed)
    symbols = extract_symbols(corpus_root)
    if not symbols:
        raise ValueError(f"No symbols found in {corpus_root}")
    files = _list_files(corpus_root)

    # Build a live pool of symbol-names we know to be "currently present".
    # As we schedule deletions/renames we update the pool so later actions
    # stay consistent (e.g. we don't rename a symbol that was already
    # deleted earlier in the script).
    live: list[Symbol] = list(symbols)

    actions: list[EditAction] = []
    for i in range(num_actions):
        r = rng.random()
        if r < 0.25 or not live:
            # add_symbol — new function in an existing file.
            file = rng.choice(files)
            new_name = f"added_{i:03d}_{rng.randint(1000, 9999)}"
            actions.append(EditAction(
                id=f"e{i+1}",
                action_type="add_symbol",
                file=file,
                target_name=new_name,
                expected_post_state={
                    "symbol": new_name, "file": file, "present": True,
                },
            ))
            live.append(Symbol(name=new_name, kind="function", file=file, line=0))

        elif r < 0.55:
            # rename_symbol — pick a live symbol, rename in place.
            s = rng.choice(live)
            new_name = f"{s.name}_renamed_{i:03d}"
            actions.append(EditAction(
                id=f"e{i+1}",
                action_type="rename_symbol",
                file=s.file,
                target_name=s.name,
                new_name=new_name,
                expected_post_state={
                    "symbol": new_name, "file": s.file, "present": True,
                    "old_symbol": s.name, "old_present": False,
                },
            ))
            # Update live pool.
            live = [x for x in live if not (x.name == s.name and x.file == s.file)]
            live.append(Symbol(name=new_name, kind=s.kind, file=s.file, line=s.line))

        elif r < 0.80:
            # move_symbol — pick a live symbol, move to a different file.
            s = rng.choice(live)
            dest = rng.choice([f for f in files if f != s.file] or files)
            actions.append(EditAction(
                id=f"e{i+1}",
                action_type="move_symbol",
                file=s.file,
                target_name=s.name,
                dest_file=dest,
                expected_post_state={
                    "symbol": s.name, "file": dest, "present": True,
                    "old_file": s.file, "old_present": False,
                },
            ))
            live = [x for x in live if not (x.name == s.name and x.file == s.file)]
            live.append(Symbol(name=s.name, kind=s.kind, file=dest, line=s.line))

        else:
            # delete_symbol
            s = rng.choice(live)
            actions.append(EditAction(
                id=f"e{i+1}",
                action_type="delete_symbol",
                file=s.file,
                target_name=s.name,
                expected_post_state={
                    "symbol": s.name, "file": s.file, "present": False,
                },
            ))
            live = [x for x in live if not (x.name == s.name and x.file == s.file)]

    return actions


def serialize(actions: list[EditAction]) -> list[dict]:
    return [asdict(a) for a in actions]


def main() -> None:
    """CLI entrypoint — regenerates benchmarks/suite/datasets/bench_4_edits.json."""
    from benchmarks.suite.corpora.scratch_fixture_corpus import ScratchFixtureCorpus
    corpus = ScratchFixtureCorpus()
    actions = generate_actions(corpus.path)
    out = Path(__file__).resolve().parents[1] / "bench_4_edits.json"
    out.write_text(json.dumps(serialize(actions), indent=2) + "\n")
    print(f"wrote {len(actions)} actions -> {out}")


if __name__ == "__main__":
    main()
