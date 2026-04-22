"""Bench-agnostic runner. Bench #0 version — iterates a symbol-lookup dataset
and calls `adapter.query_symbol` per query."""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from benchmarks.suite.contract import Adapter
from benchmarks.suite.scoring import rank_of_first_hit


@dataclass
class BenchRow:
    query_id: str
    target_symbol: str
    expected_file: str
    paths_count: int
    top_paths: list[str]
    rank: int | None
    latency_ms: float
    tokens: int
    error: str | None = None


def run_bench_0(
    adapter: Adapter,
    dataset: list[dict[str, Any]],
    limit: int,
    corpus=None,
) -> list[BenchRow]:
    """Run Bench #0 against one adapter. Returns one BenchRow per query.

    Guarantees:
      - adapter.setup(corpus) called once before any query
      - adapter.teardown() called once after all queries (even on mid-run error)
      - per-query errors captured on the row; runner does not raise
    """
    rows: list[BenchRow] = []
    adapter.setup(corpus)
    try:
        for q in dataset:
            qid = q["id"]
            sym = q["target_symbol"]
            expected = q["expected_file"]
            try:
                res = adapter.query_symbol(sym, limit=limit)
                rank = rank_of_first_hit(res.paths, expected)
                rows.append(BenchRow(
                    query_id=qid, target_symbol=sym, expected_file=expected,
                    paths_count=len(res.paths), top_paths=res.paths[:3],
                    rank=rank, latency_ms=res.latency_ms, tokens=res.tokens_used,
                    error=None,
                ))
            except Exception as e:
                rows.append(BenchRow(
                    query_id=qid, target_symbol=sym, expected_file=expected,
                    paths_count=0, top_paths=[], rank=None,
                    latency_ms=0.0, tokens=0,
                    error=f"{type(e).__name__}: {e}",
                ))
    finally:
        adapter.teardown()
    return rows


def stamp_rows(rows: list[BenchRow], adapter_name: str) -> list[dict]:
    """Serialize rows to dicts and stamp the adapter name on each. Single
    source of truth for the (`BenchRow` → jsonl dict + `adapter`) conversion."""
    stamped = []
    for r in rows:
        d = asdict(r)
        d["adapter"] = adapter_name
        stamped.append(d)
    return stamped


def rows_to_jsonl(rows: list[BenchRow], dst: Path) -> None:
    """Write one JSON object per line. Reporting reads this back."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w") as f:
        for r in rows:
            f.write(json.dumps(asdict(r)) + "\n")
