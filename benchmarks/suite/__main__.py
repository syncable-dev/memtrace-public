"""Suite CLI: `python -m benchmarks.suite run --bench 0 --adapters a,b`."""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path

from benchmarks.suite.benches.bench_0_exact_symbol import run as bench_0
from benchmarks.suite.corpora.mempalace import MempalaceCorpus
from benchmarks.suite.reporting import (
    rollup_from_jsonl, format_markdown, write_csv,
)
from benchmarks.suite.runner import stamp_rows


def _resolve_adapter(name: str):
    if name == "memtrace":
        from benchmarks.suite.adapters.memtrace import MemtraceAdapter
        return MemtraceAdapter()
    if name == "chromadb":
        from benchmarks.suite.adapters.chromadb import ChromaDBAdapter
        return ChromaDBAdapter()
    if name == "gitnexus":
        from benchmarks.suite.adapters.gitnexus import GitNexusAdapter
        return GitNexusAdapter()
    if name == "cgc":
        from benchmarks.suite.adapters.cgc import CGCAdapter
        return CGCAdapter()
    raise SystemExit(f"unknown adapter: {name}")


def main() -> int:
    p = argparse.ArgumentParser(prog="benchmarks.suite")
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("run", help="Run a bench against one or more adapters")
    rp.add_argument("--bench", required=True, choices=["0", "3", "4", "5"])
    rp.add_argument("--adapters", required=True,
                    help="comma-separated: memtrace,chromadb,gitnexus,cgc")
    rp.add_argument("--limit", type=int, default=10)
    rp.add_argument("--max-queries", type=int,
                    default=int(os.environ.get("MAX_QUERIES", "1000")))
    rp.add_argument("--out", default=None,
                    help="Output directory. Defaults to benchmarks/suite/results/bench_<N>")

    args = p.parse_args()

    # Benches #3/#4/#5 are infra-ready but execution is deferred to a
    # follow-up. We print an informative message rather than silently
    # running an incomplete bench.
    if args.cmd == "run" and args.bench in ("3", "4", "5"):
        msg = {
            "3": ("Bench #3 infrastructure ready (adapter methods, scoring, "
                  "ground-truth, runner). To execute: wire a shell that "
                  "calls benches.bench_3_graph_queries.run_with_adapter per "
                  "adapter — see run.py. Not yet callable from this CLI."),
            "4": ("Bench #4 corpus + edit-script ready. Inner edit-application "
                  "loop in benches/bench_4_incremental/run.py is the remaining "
                  "TODO. Not yet callable from this CLI."),
            "5": ("Bench #5 is GATED behind RUN_AGENT_BENCH=1 and its agent "
                  "driver is intentionally unimplemented here — it spends LLM "
                  "credits. Not yet callable from this CLI."),
        }[args.bench]
        print(f"Bench #{args.bench} not yet executable — run not implemented")
        print(msg)
        return 2

    if args.cmd == "run" and args.bench == "0":
        args.out = args.out or "benchmarks/suite/results/bench_0"
        corpus = MempalaceCorpus()
        dataset = bench_0.load_dataset()[: args.max_queries]
        out_dir = Path(args.out)

        print(f"Bench #0 — {len(dataset)} queries, adapters: {args.adapters}")
        all_jsonl = out_dir / f"combined-{time.strftime('%Y%m%dT%H%M%S')}.jsonl"
        all_jsonl.parent.mkdir(parents=True, exist_ok=True)
        combined = all_jsonl.open("w")
        try:
            for name in args.adapters.split(","):
                name = name.strip()
                if not name:
                    continue
                print(f"\n── {name} ──")
                a = _resolve_adapter(name)
                rows = bench_0.run_with_adapter(a, dataset, out_dir, args.limit, corpus)
                for d in stamp_rows(rows, name):
                    combined.write(json.dumps(d) + "\n")
                print(f"  {len(rows)} rows")
        finally:
            combined.close()

        rollup = rollup_from_jsonl(all_jsonl)
        md = format_markdown(
            rollup,
            bench_id=bench_0.BENCH_ID,
            primary_axis=bench_0.PRIMARY_AXIS,
            dataset_version=bench_0.DATASET_VERSION,
            n_queries=len(dataset),
        )
        md_path = out_dir / "rollup.md"
        md_path.write_text(md)
        write_csv(rollup, out_dir / "rollup.csv")
        print("\n" + md)
        print(f"✓ wrote {md_path}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
