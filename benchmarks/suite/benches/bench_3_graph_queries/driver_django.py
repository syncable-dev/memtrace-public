"""Bench #3 — Django variant.

Same runner as `driver.py` but loads Django's pyright ground truth and
points the Memtrace adapter at `repo_id="django"`. All other adapters
don't care about repo_id — they read the filesystem directly — so only
Memtrace gets overridden.

Run:
    benchmarks/.venv/bin/python -m benchmarks.suite.benches.bench_3_graph_queries.driver_django
"""
from __future__ import annotations
import csv
import json
import os
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path

from benchmarks.suite.adapters.memtrace import MemtraceAdapter
from benchmarks.suite.adapters.chromadb import ChromaDBAdapter
from benchmarks.suite.adapters.gitnexus import GitNexusAdapter
from benchmarks.suite.adapters.cgc import CGCAdapter
from benchmarks.suite.corpora.django import DjangoCorpus
from benchmarks.suite.benches.bench_3_graph_queries import run as bench_3
from benchmarks.suite.benches.bench_3_graph_queries.driver import (
    GraphSummary, summarise, format_markdown,
)


DJANGO_GT = Path(__file__).resolve().parents[3] / "suite" / "datasets" / "bench_3_graph_django.json"
OUT_DIR = Path(__file__).resolve().parents[3] / "suite" / "results" / "bench_3_full_django"


def main():
    if not DJANGO_GT.exists():
        raise SystemExit(
            f"Django pyright ground truth not found at {DJANGO_GT}.\n"
            f"Generate it first: benchmarks/.venv/bin/python -m benchmarks.suite.datasets.generators.pyright_graph_django"
        )

    dataset = json.load(DJANGO_GT.open())
    print(f"Bench #3 (Django) — {len(dataset)} symbols, 4 adapters\n")
    print("Note: ChromaDB returns NotSupported instantly (no graph support).\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    corpus = DjangoCorpus()
    all_rows = {}

    adapters = [
        # Memtrace scoped to the django repo_id explicitly — graph methods
        # pass repo_id=self.repo_id.
        ("memtrace", MemtraceAdapter(repo_id="django")),
        ("chromadb", ChromaDBAdapter(collection_name="bench_3_django_noop")),
        ("gitnexus", GitNexusAdapter()),
    ]
    if os.environ.get("BENCH_3_SKIP_CGC") != "1":
        adapters.append(("cgc", CGCAdapter()))
    else:
        print("⚠ Skipping CGC (BENCH_3_SKIP_CGC=1)\n")

    for name, adapter in adapters:
        print(f"── {name} ──")
        t0 = time.time()
        rows = bench_3.run_with_adapter(adapter, dataset, OUT_DIR, corpus=corpus)
        elapsed = time.time() - t0
        print(f"  {len(rows)} rows in {elapsed:.1f}s")
        all_rows[name] = rows

    summaries = [summarise(name, rows) for name, rows in all_rows.items()]

    md = format_markdown(summaries)
    md = md.replace("Bench #3 — Graph Queries",
                    "Bench #3 — Graph Queries (Django generalization)")
    (OUT_DIR / "rollup.md").write_text(md)

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
