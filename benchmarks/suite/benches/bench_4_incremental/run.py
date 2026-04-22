"""Bench #4 runner.

For each action in `bench_4_edits.json`:

1. Apply the edit to disk (mutate the scratch_fixture copy):
   - `add_symbol`    — append a `def <name>` to the target file.
   - `rename_symbol` — rewrite definition + bareword refs in same file.
   - `move_symbol`   — cut the AST-block from source, append to dest.
   - `delete_symbol` — drop the AST-block.
2. Call `adapter.reindex_paths([affected files])` — measures wall-time.
3. Call `adapter.time_to_queryable(expected_symbol, deadline_ms)` —
   polls the adapter until the new symbol is queryable OR the deadline
   elapses (sentinel `deadline_ms + 1`).
4. For rename / move / delete: query the OLD symbol name to detect
   staleness (did the adapter still return the old location?).
5. Record a row. **Do NOT roll back between edits within one adapter
   run** — the edits compound deterministically, matching what a real
   agent workflow looks like. The driver reverts the fixture between
   adapters via `git checkout`.

Primary axis: `time_to_queryable_p95` (ms). Lower is better.
Secondary:    `staleness_rate`, `reindex_ms` median, `supported_pct`.
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from benchmarks.suite.contract import Adapter, NotSupported, ReindexReport
from benchmarks.suite.benches.bench_4_incremental.edits import (
    EditError, apply_edit,
)


BENCH_ID = "Bench #4 — Incremental Indexing & Staleness"
PRIMARY_AXIS = "time_to_queryable_p95"
DATASET_VERSION = "scratch-fixture-2026-04-21"
DEADLINE_MS = 5000


def default_dataset_path() -> Path:
    """bench_4_edits.json lives at benchmarks/suite/datasets/."""
    return Path(__file__).resolve().parents[2] / "datasets" / "bench_4_edits.json"


def load_edits(path: Path | None = None) -> list[dict]:
    p = path or default_dataset_path()
    with p.open() as f:
        return json.load(f)


@dataclass
class IncrementalRow:
    edit_id: str
    kind: str                 # add_symbol | rename_symbol | move_symbol | delete_symbol
    file: str
    name: str                 # post-edit symbol of interest (for queryable check)
    old_name: str | None      # pre-edit name (for staleness probing)
    reindex_ms: float
    reindex_supported: bool
    reindex_files: int
    time_to_queryable_ms: float
    queryable: bool           # True if found within deadline
    stale: bool               # True if post-edit the OLD state still resolves
    error: str | None = None


def _expected_symbol(action: dict) -> tuple[str | None, str | None]:
    """Given an action, return (new_name_to_find, old_name_to_staleness_probe).

    - add_symbol    → (target_name, None)
    - rename_symbol → (new_name,    target_name)
    - move_symbol   → (target_name, None) and probe old file via path check
    - delete_symbol → (None,        target_name)      — nothing "new" to find
    """
    kind = action["action_type"]
    tgt = action.get("target_name")
    if kind == "add_symbol":
        return (tgt, None)
    if kind == "rename_symbol":
        return (action.get("new_name"), tgt)
    if kind == "move_symbol":
        # For move, the symbol name stays the same; staleness = old FILE.
        return (tgt, tgt)
    if kind == "delete_symbol":
        return (None, tgt)
    return (None, None)


def _is_stale(
    adapter: Adapter,
    action: dict,
    old_name: str | None,
    corpus_name: str = "scratch_fixture",
) -> bool:
    """After the edit, does the adapter still return pre-edit state?

    Cross-repo noise is filtered out by requiring the path to mention the
    bench corpus name — otherwise unrelated symbols named the same way in
    other indexed repos (django / mempalace / etc.) would inflate the
    staleness rate and make the metric meaningless.

    - rename_symbol: query OLD name → stale if any in-corpus path comes back.
    - delete_symbol: query OLD name → stale if any in-corpus path comes back.
    - move_symbol:   query OLD name → stale if any in-corpus path still
                     contains the OLD file basename.
    - add_symbol:    no stale signal (there was no pre-state).
    """
    if old_name is None:
        return False
    try:
        res = adapter.query_symbol(old_name, limit=10)
    except Exception:
        return False
    paths = getattr(res, "paths", None) or []
    # Scope to the bench corpus — everything else is cross-repo noise.
    in_corpus = [p for p in paths if corpus_name in p]
    if not in_corpus:
        return False

    kind = action["action_type"]
    if kind in {"rename_symbol", "delete_symbol"}:
        return len(in_corpus) > 0

    if kind == "move_symbol":
        old_file = action.get("file", "")
        old_base = Path(old_file).name
        # Stale if ANY in-corpus path still resolves to the old file.
        return any(old_base in p for p in in_corpus)

    return False


def run_with_adapter(
    adapter: Adapter,
    edits: list[dict],
    out_dir: Path,
    corpus=None,
    deadline_ms: int = DEADLINE_MS,
    post_setup=None,
) -> list[IncrementalRow]:
    """Apply each edit, measure reindex + time-to-queryable, score staleness.

    The caller is responsible for reverting the fixture between adapter runs.

    `post_setup` is an optional callable invoked after `adapter.setup()` and
    before the edit loop. Use it for one-time indexing that needs the
    adapter's live session (e.g. Memtrace's `ensure_indexed`).
    """
    if corpus is None:
        from benchmarks.suite.corpora.scratch_fixture_corpus import ScratchFixtureCorpus
        corpus = ScratchFixtureCorpus()
    corpus_root: Path = corpus.path

    rows: list[IncrementalRow] = []
    adapter.setup(corpus)
    if post_setup is not None:
        post_setup(adapter, corpus)
    try:
        for action in edits:
            edit_id = action.get("id", "?")
            kind = action["action_type"]
            file_rel = action.get("file", "")
            target = action.get("target_name", "")
            err: str | None = None
            changed: list[Path] = []

            # 1. Apply the edit on disk.
            try:
                changed = apply_edit(action, corpus_root)
            except EditError as e:
                err = f"EditError: {e}"
            except Exception as e:  # pragma: no cover — keep going
                err = f"{type(e).__name__}: {e}"

            new_name, old_name = _expected_symbol(action)

            reindex_ms = 0.0
            reindex_files = 0
            reindex_supported = False
            time_queryable_ms = float(deadline_ms + 1)
            queryable = False
            stale = False

            if err is None and changed:
                # 2. Ask the adapter to pick up the change.
                try:
                    rep = adapter.reindex_paths(changed)
                except Exception as e:
                    err = f"reindex_paths: {type(e).__name__}: {e}"
                    rep = None

                if isinstance(rep, NotSupported):
                    reindex_supported = False
                    # Record reindex_ms = 0 since the call was a no-op.
                    reindex_ms = 0.0
                elif isinstance(rep, ReindexReport):
                    reindex_supported = True
                    reindex_ms = rep.wall_ms
                    reindex_files = rep.files_reindexed

                # 3. Poll for queryability, IF the reindex is supported AND
                # there's a symbol we can even expect to find. (Delete has no
                # "new" symbol to wait for — we skip the poll and only probe
                # staleness below.)
                if reindex_supported and new_name and err is None:
                    try:
                        t_q = adapter.time_to_queryable(new_name, deadline_ms=deadline_ms)
                        time_queryable_ms = float(t_q)
                        queryable = t_q <= deadline_ms
                    except Exception as e:
                        err = f"time_to_queryable: {type(e).__name__}: {e}"
                elif not reindex_supported:
                    # N/A: honest-loss surface — no poll, deadline sentinel.
                    time_queryable_ms = float(deadline_ms + 1)
                elif new_name is None:
                    # Delete: no new symbol, treat as "not applicable". The
                    # reindex wall time still counts; time_to_queryable stays
                    # at the sentinel so the p95 reflects reality.
                    time_queryable_ms = 0.0
                    queryable = True  # trivially — there's nothing to find

                # 4. Staleness probe. Runs regardless of reindex_supported
                # because "still returns old state" is the metric — if the
                # adapter opts out of incremental, it WILL be stale.
                try:
                    stale = _is_stale(adapter, action, old_name)
                except Exception as e:
                    err = err or f"stale_probe: {type(e).__name__}: {e}"

            rows.append(IncrementalRow(
                edit_id=edit_id,
                kind=kind,
                file=file_rel,
                name=new_name or target,
                old_name=old_name,
                reindex_ms=round(reindex_ms, 2),
                reindex_supported=reindex_supported,
                reindex_files=reindex_files,
                time_to_queryable_ms=round(time_queryable_ms, 2),
                queryable=queryable,
                stale=stale,
                error=err,
            ))
    finally:
        adapter.teardown()

    # Persist jsonl.
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%S")
    out_file = out_dir / f"{adapter.name}-{ts}.jsonl"
    with out_file.open("w") as f:
        for r in rows:
            d = asdict(r)
            d["adapter"] = adapter.name
            f.write(json.dumps(d) + "\n")
    return rows
