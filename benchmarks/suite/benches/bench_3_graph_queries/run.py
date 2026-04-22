"""Bench #3 runner.

For each ground-truth triple {symbol, callers[], callees[], impact[]}:
  1. call adapter.callers_of(symbol) → compute precision/recall vs callers[]
  2. call adapter.callees_of(symbol) → compute precision/recall vs callees[]
  3. call adapter.impact_of(symbol)  → compute recall vs impact[]
Also, once per adapter:
  4. call adapter.find_dead_code() → compute f1 vs a disclosed subset of
     known-dead symbols (see Bench #3 README when generated — the dead-code
     ground truth is a separate list, since pyright doesn't produce it
     directly).

`NotSupported` responses are recorded as-is on the row (`supported: False`)
and excluded from the per-axis averages — the reporter surfaces them as
an N/A column. This is the honest-loss surface for vector DBs.

Latency is recorded per call but is a secondary axis; primary reporting
is `callers_of.recall` (average across supported rows).
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from benchmarks.suite.contract import Adapter, GraphResult, NotSupported
from benchmarks.suite.scoring import precision, recall, f1


BENCH_ID = "Bench #3 — Graph Queries"
PRIMARY_AXIS = "callers_of.recall"
DATASET_VERSION = "pyright-graph-2026-04-21"   # bumped when bench_3_graph.json regenerated


def default_dataset_path() -> Path:
    return Path(__file__).resolve().parents[2] / "datasets" / "bench_3_graph.json"


def load_dataset(path: Path | None = None) -> list[dict]:
    p = path or default_dataset_path()
    if not p.exists():
        raise FileNotFoundError(
            f"bench_3_graph.json not found at {p}. Generate it with "
            f"`python -m benchmarks.suite.datasets.generators.pyright_graph` "
            f"(requires pyright installed globally — see generator docstring)."
        )
    with p.open() as f:
        return json.load(f)


@dataclass
class GraphRow:
    symbol_id: str
    symbol: str
    callers_supported: bool
    callers_precision: float
    callers_recall: float
    callers_latency_ms: float
    callees_supported: bool
    callees_precision: float
    callees_recall: float
    callees_latency_ms: float
    impact_supported: bool
    impact_recall: float
    impact_latency_ms: float
    error: str | None = None


def _names_from_gold(gold_entries: list[dict]) -> set[str]:
    """Flatten ground-truth entries ({name, file, line}) into a name-only set.

    Bench #3 scores on symbol names — not (name, file) pairs — because
    adapters disagree on path formats. If disambiguation becomes necessary
    we upgrade the schema; for v1 name-only is honest AND conservative.
    """
    return {e.get("name", "") for e in gold_entries if e.get("name")}


def _names_from_graph(g: GraphResult) -> set[str]:
    return {s.name for s in g.nodes if s.name}


def _score_pair(
    fn_result,
    gold_names: set[str],
) -> tuple[bool, float, float, float]:
    """Returns (supported, precision, recall, latency_ms)."""
    if isinstance(fn_result, NotSupported):
        return (False, 0.0, 0.0, 0.0)
    retrieved = _names_from_graph(fn_result)
    return (True, precision(retrieved, gold_names),
            recall(retrieved, gold_names), fn_result.latency_ms)


def run_with_adapter(
    adapter: Adapter,
    dataset: list[dict[str, Any]],
    out_dir: Path,
    corpus=None,
) -> list[GraphRow]:
    """Iterate ground-truth triples; call callers_of/callees_of/impact_of."""
    rows: list[GraphRow] = []
    adapter.setup(corpus)
    try:
        for entry in dataset:
            qid = entry.get("id", "?")
            sym = entry.get("symbol", "")
            try:
                callers_gold = _names_from_gold(entry.get("callers", []))
                callees_gold = _names_from_gold(entry.get("callees", []))
                impact_gold  = _names_from_gold(entry.get("impact",  []))

                cres = adapter.callers_of(sym)
                csup, cprec, crec, clat = _score_pair(cres, callers_gold)

                eres = adapter.callees_of(sym)
                esup, eprec, erec, elat = _score_pair(eres, callees_gold)

                ires = adapter.impact_of(sym)
                isup = not isinstance(ires, NotSupported)
                irec = recall(_names_from_graph(ires), impact_gold) if isup else 0.0
                ilat = ires.latency_ms if isup else 0.0

                rows.append(GraphRow(
                    symbol_id=qid, symbol=sym,
                    callers_supported=csup, callers_precision=cprec,
                    callers_recall=crec, callers_latency_ms=clat,
                    callees_supported=esup, callees_precision=eprec,
                    callees_recall=erec, callees_latency_ms=elat,
                    impact_supported=isup, impact_recall=irec,
                    impact_latency_ms=ilat,
                ))
            except Exception as e:
                rows.append(GraphRow(
                    symbol_id=qid, symbol=sym,
                    callers_supported=False, callers_precision=0.0,
                    callers_recall=0.0, callers_latency_ms=0.0,
                    callees_supported=False, callees_precision=0.0,
                    callees_recall=0.0, callees_latency_ms=0.0,
                    impact_supported=False, impact_recall=0.0,
                    impact_latency_ms=0.0,
                    error=f"{type(e).__name__}: {e}",
                ))
    finally:
        adapter.teardown()

    # Persist.
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%S")
    out_file = out_dir / f"{adapter.name}-{ts}.jsonl"
    with out_file.open("w") as f:
        for r in rows:
            d = asdict(r)
            d["adapter"] = adapter.name
            f.write(json.dumps(d) + "\n")
    return rows
