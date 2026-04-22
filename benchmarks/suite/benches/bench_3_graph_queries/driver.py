"""Bench #3 driver — runs the graph-query bench against all 4 adapters
and produces a rollup markdown + CSV. Saves each adapter's per-query jsonl
under `benchmarks/suite/results/bench_3_full/`.

Run:
    benchmarks/.venv/bin/python -m benchmarks.suite.benches.bench_3_graph_queries.driver

Environment gates:
    BENCH_3_SKIP_CGC=1   → skip CGC (expensive; ~15 min on 200 symbols)
"""
from __future__ import annotations
import csv
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from benchmarks.suite.adapters.memtrace import MemtraceAdapter
from benchmarks.suite.adapters.chromadb import ChromaDBAdapter
from benchmarks.suite.adapters.gitnexus import GitNexusAdapter
from benchmarks.suite.adapters.cgc import CGCAdapter
from benchmarks.suite.corpora.mempalace import MempalaceCorpus
from benchmarks.suite.benches.bench_3_graph_queries import run as bench_3


OUT_DIR = Path(__file__).resolve().parents[3] / "results" / "bench_3_full"


@dataclass
class GraphSummary:
    adapter: str
    n_queries: int
    callers_supported_pct: float
    callers_recall: float
    callers_precision: float
    callees_supported_pct: float
    callees_recall: float
    callees_precision: float
    impact_supported_pct: float
    impact_recall: float
    callers_f1: float
    avg_latency_ms: float
    errors: int


def summarise(name: str, rows: list) -> GraphSummary:
    n = len(rows)
    if n == 0:
        return GraphSummary(name, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    cs = [r for r in rows if r.callers_supported]
    es = [r for r in rows if r.callees_supported]
    isu = [r for r in rows if r.impact_supported]

    def avg(xs, getter):
        v = [getter(x) for x in xs]
        return round(statistics.mean(v), 4) if v else 0.0

    callers_recall = avg(cs, lambda r: r.callers_recall)
    callers_prec   = avg(cs, lambda r: r.callers_precision)
    callers_f1 = round(
        2 * callers_prec * callers_recall / (callers_prec + callers_recall)
        if (callers_prec + callers_recall) > 0 else 0.0,
        4,
    )
    latencies = [
        l for r in rows
        for l in (r.callers_latency_ms, r.callees_latency_ms, r.impact_latency_ms)
        if l > 0
    ]
    return GraphSummary(
        adapter=name,
        n_queries=n,
        callers_supported_pct=round(len(cs) / n * 100, 1),
        callers_recall=callers_recall,
        callers_precision=callers_prec,
        callees_supported_pct=round(len(es) / n * 100, 1),
        callees_recall=avg(es, lambda r: r.callees_recall),
        callees_precision=avg(es, lambda r: r.callees_precision),
        impact_supported_pct=round(len(isu) / n * 100, 1),
        impact_recall=avg(isu, lambda r: r.impact_recall),
        callers_f1=callers_f1,
        avg_latency_ms=round(statistics.mean(latencies), 2) if latencies else 0.0,
        errors=sum(1 for r in rows if r.error),
    )


def format_markdown(summaries: list[GraphSummary]) -> str:
    lines = [
        f"# {bench_3.BENCH_ID}",
        "",
        f"**Primary axis:** `{bench_3.PRIMARY_AXIS}`",
        f"**Queries:** {summaries[0].n_queries if summaries else 0}",
        f"**Dataset version:** {bench_3.DATASET_VERSION}",
        f"**Ground truth:** pyright LSP (`callHierarchy/incomingCalls`, `outgoingCalls`)",
        "",
        "| Adapter | Callers supported | **Callers recall** | Callers precision | Callers F1 | Callees recall | Impact recall | Avg latency | Errors |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summaries:
        lines.append(
            f"| {s.adapter} | {s.callers_supported_pct:.1f}% | "
            f"**{s.callers_recall:.3f}** | {s.callers_precision:.3f} | "
            f"{s.callers_f1:.3f} | {s.callees_recall:.3f} | "
            f"{s.impact_recall:.3f} | {s.avg_latency_ms:.1f} ms | {s.errors} |"
        )

    supported = [s for s in summaries if s.callers_supported_pct > 0]
    if supported:
        winner = max(supported, key=lambda s: s.callers_recall)
        runners = [s for s in supported if s.adapter != winner.adapter]
        best_other = max(runners, key=lambda s: s.callers_recall) if runners else None
        lines.extend([
            "",
            "## Primary axis result",
            "",
            (f"✅ **{winner.adapter} wins** `{bench_3.PRIMARY_AXIS}` "
             f"(recall {winner.callers_recall:.3f} vs {best_other.callers_recall:.3f})"
             if best_other else f"✅ **{winner.adapter} ran solo** (no comparison)"),
        ])

    # Honest-loss section: which adapters are out of the running on graph?
    na = [s for s in summaries if s.callers_supported_pct == 0]
    if na:
        lines.extend([
            "",
            "## NotSupported (by design)",
            "",
            "These adapters return `NotSupported` for graph queries — they cannot compete on this bench:",
            "",
        ])
        for s in na:
            lines.append(f"- `{s.adapter}` — no graph support")

    return "\n".join(lines) + "\n"


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dataset = bench_3.load_dataset()
    print(f"Bench #3 — {len(dataset)} ground-truth symbols, 4 adapters\n")
    print("Note: ChromaDB returns NotSupported instantly (no graph support).\n")

    corpus = MempalaceCorpus()
    all_rows = {}

    adapters_to_run = [
        ("memtrace", MemtraceAdapter()),
        ("chromadb", ChromaDBAdapter(collection_name="bench_3_noop")),
        ("gitnexus", GitNexusAdapter()),
    ]
    if os.environ.get("BENCH_3_SKIP_CGC") != "1":
        adapters_to_run.append(("cgc", CGCAdapter()))
    else:
        print("⚠ Skipping CGC (BENCH_3_SKIP_CGC=1)\n")

    for name, adapter in adapters_to_run:
        print(f"── {name} ──")
        t0 = time.time()
        rows = bench_3.run_with_adapter(adapter, dataset, OUT_DIR, corpus=corpus)
        elapsed = time.time() - t0
        print(f"  {len(rows)} rows in {elapsed:.1f}s")
        all_rows[name] = rows

    summaries = [summarise(name, rows) for name, rows in all_rows.items()]

    md = format_markdown(summaries)
    (OUT_DIR / "rollup.md").write_text(md)

    # CSV
    fields = list(GraphSummary.__dataclass_fields__.keys())
    with (OUT_DIR / "rollup.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for s in summaries:
            w.writerow([asdict(s)[k] for k in fields])

    print("\n" + md)
    print(f"\n✓ wrote {OUT_DIR}/rollup.md and rollup.csv")


if __name__ == "__main__":
    sys.exit(main())
