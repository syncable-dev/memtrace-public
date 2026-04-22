"""Bench #0 — Exact-Symbol Lookup. Wraps fair/dataset.json + delegates to
the shared suite runner so all four adapters share one reporting pipeline."""
from __future__ import annotations
import json
import time
from pathlib import Path

from benchmarks.suite.contract import Adapter
from benchmarks.suite.runner import run_bench_0, stamp_rows, BenchRow


BENCH_ID = "Bench #0 — Exact-Symbol Lookup"
PRIMARY_AXIS = "acc_at_1_pct"
DATASET_VERSION = "fair-2026-04-20"   # bump when fair/dataset.json regenerated


def default_dataset_path() -> Path:
    """Reuse fair/'s tool-neutral AST dataset. No duplication."""
    return Path(__file__).resolve().parents[3] / "fair" / "dataset.json"


def load_dataset(path: Path | None = None) -> list[dict]:
    p = path or default_dataset_path()
    with p.open() as f:
        return json.load(f)


def run_with_adapter(
    adapter: Adapter,
    dataset: list[dict],
    out_dir: Path,
    limit: int = 10,
    corpus=None,
) -> list[BenchRow]:
    """Run Bench #0 against one adapter and persist rows to jsonl."""
    rows = run_bench_0(adapter, dataset, limit=limit, corpus=corpus)
    stamped = stamp_rows(rows, adapter.name)

    ts = time.strftime("%Y%m%dT%H%M%S")
    out_file = out_dir / f"{adapter.name}-{ts}.jsonl"
    out_dir.mkdir(parents=True, exist_ok=True)
    with out_file.open("w") as f:
        for d in stamped:
            f.write(json.dumps(d) + "\n")
    return rows
