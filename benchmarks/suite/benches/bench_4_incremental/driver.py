"""Bench #4 driver — runs the incremental-indexing bench against all 4
adapters and produces a rollup markdown + CSV. Saves each adapter's
per-edit jsonl under `benchmarks/suite/results/bench_4_full/`.

The driver reverts the scratch_fixture between adapters with:

    git -C <worktree> checkout HEAD -- benchmarks/suite/corpora/scratch_fixture/

so each adapter sees the same starting corpus. It relies on the worktree
being clean when the driver starts (any prior edits to scratch_fixture
will be checked out).

Run:
    benchmarks/.venv/bin/python -m benchmarks.suite.benches.bench_4_incremental.driver

Environment gates:
    BENCH_4_SKIP_CGC=1       → skip CGC (its re-index CLI is slow)
    BENCH_4_SKIP_GITNEXUS=1  → skip GitNexus (requires eval-server up)
    BENCH_4_DEADLINE_MS=N    → override queryable deadline (default 5000)
"""
from __future__ import annotations
import csv
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from benchmarks.suite.adapters.memtrace import MemtraceAdapter
from benchmarks.suite.adapters.chromadb import ChromaDBAdapter
from benchmarks.suite.adapters.gitnexus import GitNexusAdapter
from benchmarks.suite.adapters.cgc import CGCAdapter
from benchmarks.suite.corpora.scratch_fixture_corpus import ScratchFixtureCorpus
from benchmarks.suite.benches.bench_4_incremental import run as bench_4
from benchmarks.suite.scoring import (
    latency_stats, staleness_rate, time_to_queryable_p95,
)


OUT_DIR = Path(__file__).resolve().parents[2] / "results" / "bench_4_full"
CORPUS_REL = "benchmarks/suite/corpora/scratch_fixture"
# Worktree root (three levels up from the driver).
WORKTREE_ROOT = Path(__file__).resolve().parents[4]


@dataclass
class IncrementalSummary:
    adapter: str
    n_edits: int
    supported_pct: float
    queryable_pct: float
    time_to_queryable_p50: float
    time_to_queryable_p95: float
    time_to_queryable_p99: float
    reindex_ms_median: float
    reindex_ms_mean: float
    staleness_rate: float
    errors: int


def summarise(name: str, rows: list[bench_4.IncrementalRow]) -> IncrementalSummary:
    n = len(rows)
    if n == 0:
        return IncrementalSummary(name, 0, 0, 0, float("inf"), float("inf"), float("inf"),
                                  0, 0, 0, 0)
    supported = [r for r in rows if r.reindex_supported]
    # Only count samples where we actually ran a poll (excludes delete + unsupported).
    pollable = [r for r in rows if r.reindex_supported and r.kind != "delete_symbol"]
    queryable_ms = [r.time_to_queryable_ms for r in pollable]
    reindex_ms = [r.reindex_ms for r in supported]

    lat = latency_stats(queryable_ms) if queryable_ms else {
        "p50": float("inf"), "p95": float("inf"), "p99": float("inf"),
    }
    # Explicit p95 via the scoring primitive (same data, different accessor).
    p95 = time_to_queryable_p95(queryable_ms) if queryable_ms else float("inf")

    return IncrementalSummary(
        adapter=name,
        n_edits=n,
        supported_pct=round(len(supported) / n * 100, 1),
        queryable_pct=round(
            sum(1 for r in pollable if r.queryable) / len(pollable) * 100, 1
        ) if pollable else 0.0,
        time_to_queryable_p50=round(lat["p50"], 2),
        time_to_queryable_p95=round(p95, 2),
        time_to_queryable_p99=round(lat["p99"], 2),
        reindex_ms_median=round(statistics.median(reindex_ms), 2) if reindex_ms else 0.0,
        reindex_ms_mean=round(statistics.mean(reindex_ms), 2) if reindex_ms else 0.0,
        staleness_rate=round(
            staleness_rate(sum(1 for r in rows if r.stale), n), 4
        ),
        errors=sum(1 for r in rows if r.error),
    )


def format_markdown(summaries: list[IncrementalSummary]) -> str:
    n = summaries[0].n_edits if summaries else 0
    lines = [
        f"# {bench_4.BENCH_ID}",
        "",
        f"**Primary axis:** `{bench_4.PRIMARY_AXIS}` (lower is better)",
        f"**Edits:** {n}",
        f"**Dataset version:** {bench_4.DATASET_VERSION}",
        f"**Deadline:** {bench_4.DEADLINE_MS} ms per edit",
        f"**Corpus:** `scratch_fixture` (21 hand-authored Python files, task-queue domain)",
        "",
        "| Adapter | Supported | Queryable | **t_queryable p95** | p50 | p99 | Reindex p50 ms | Staleness | Errors |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summaries:
        p95 = "N/A" if s.time_to_queryable_p95 == float("inf") else f"{s.time_to_queryable_p95:.1f}"
        p50 = "N/A" if s.time_to_queryable_p50 == float("inf") else f"{s.time_to_queryable_p50:.1f}"
        p99 = "N/A" if s.time_to_queryable_p99 == float("inf") else f"{s.time_to_queryable_p99:.1f}"
        lines.append(
            f"| {s.adapter} | {s.supported_pct:.1f}% | {s.queryable_pct:.1f}% | "
            f"**{p95} ms** | {p50} ms | {p99} ms | "
            f"{s.reindex_ms_median:.1f} | {s.staleness_rate:.3f} | {s.errors} |"
        )

    # Winner: lowest p95 among adapters with at least one queryable sample.
    eligible = [s for s in summaries
                if s.time_to_queryable_p95 != float("inf") and s.queryable_pct > 0]
    if eligible:
        winner = min(eligible, key=lambda s: s.time_to_queryable_p95)
        runners = [s for s in eligible if s.adapter != winner.adapter]
        best_other = min(runners, key=lambda s: s.time_to_queryable_p95) if runners else None
        lines.extend([
            "",
            "## Primary axis result",
            "",
            (f"**{winner.adapter} wins** `{bench_4.PRIMARY_AXIS}` "
             f"(p95 {winner.time_to_queryable_p95:.1f} ms vs "
             f"{best_other.time_to_queryable_p95:.1f} ms)"
             if best_other else f"**{winner.adapter} ran solo** (no comparison)"),
        ])

    # Honest-loss section.
    na = [s for s in summaries if s.supported_pct < 1.0]
    if na:
        lines.extend([
            "",
            "## NotSupported / N/A (by design)",
            "",
            "Adapters that return `NotSupported` for `reindex_paths` cannot compete on "
            "time-to-queryable — the bench's primary axis is incremental freshness:",
            "",
        ])
        for s in na:
            lines.append(f"- `{s.adapter}` — {s.supported_pct:.1f}% reindex supported")

    return "\n".join(lines) + "\n"


def _revert_fixture() -> None:
    """Clean up scratch_fixture so the next adapter starts from HEAD state."""
    subprocess.run(
        ["git", "checkout", "HEAD", "--", CORPUS_REL],
        cwd=str(WORKTREE_ROOT),
        check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Also git-clean any files we may have CREATED (move_symbol to new files).
    subprocess.run(
        ["git", "clean", "-fd", "--", CORPUS_REL],
        cwd=str(WORKTREE_ROOT),
        check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_pre_index(name: str):
    """Build a `post_setup` callback for the given adapter.

    - memtrace: call index_directory via the live MCP session + poll.
    - cgc: run `cgc index <path>` on the whole corpus (one-shot CLI).
    - chromadb: handled by its own setup() (re-embeds on first use).
    - gitnexus: server-based, nothing to do.
    """
    from benchmarks.suite.adapters.memtrace import MemtraceAdapter
    from benchmarks.suite.adapters.cgc import CGCAdapter

    def hook(adapter, corpus):
        if isinstance(adapter, MemtraceAdapter):
            print(f"  [{name}] indexing scratch_fixture (structure only)...")
            t0 = time.time()
            status = adapter.ensure_indexed(corpus.path, timeout_s=300)
            print(f"  [{name}] index complete in {time.time() - t0:.1f}s "
                  f"(state={status.get('state') or status.get('status')})")
        elif isinstance(adapter, CGCAdapter):
            if not adapter.binary.exists():
                print(f"  [{name}] cgc binary missing — skipping pre-index")
                return
            print(f"  [{name}] running `cgc index` on scratch_fixture...")
            t0 = time.time()
            try:
                subprocess.run(
                    [str(adapter.binary), "index", str(corpus.path)],
                    capture_output=True, text=True, timeout=180,
                    env={**os.environ, "CI": "1", "NO_COLOR": "1", "TERM": "dumb"},
                )
            except Exception as e:
                print(f"  [{name}] cgc index error: {e}")
            print(f"  [{name}] index complete in {time.time() - t0:.1f}s")

    return hook


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    edits = bench_4.load_edits()
    deadline_ms = int(os.environ.get("BENCH_4_DEADLINE_MS", str(bench_4.DEADLINE_MS)))

    print(f"Bench #4 — {len(edits)} edits, deadline {deadline_ms} ms")
    print(f"  corpus: {CORPUS_REL}")
    print(f"  worktree: {WORKTREE_ROOT}\n")

    corpus = ScratchFixtureCorpus()
    all_rows: dict[str, list] = {}

    adapters_to_run = [
        ("memtrace", MemtraceAdapter(repo_id="scratch_fixture")),
        ("chromadb", ChromaDBAdapter(collection_name="bench_4")),
    ]
    if os.environ.get("BENCH_4_SKIP_GITNEXUS") != "1":
        adapters_to_run.append(("gitnexus", GitNexusAdapter()))
    else:
        print("Skipping GitNexus (BENCH_4_SKIP_GITNEXUS=1)\n")
    if os.environ.get("BENCH_4_SKIP_CGC") != "1":
        adapters_to_run.append(("cgc", CGCAdapter()))
    else:
        print("Skipping CGC (BENCH_4_SKIP_CGC=1)\n")

    for name, adapter in adapters_to_run:
        print(f"── {name} ──")
        _revert_fixture()
        t0 = time.time()
        try:
            rows = bench_4.run_with_adapter(
                adapter, edits, OUT_DIR, corpus=corpus, deadline_ms=deadline_ms,
                post_setup=_make_pre_index(name),
            )
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            rows = []
        elapsed = time.time() - t0
        print(f"  {len(rows)} rows in {elapsed:.1f}s")
        all_rows[name] = rows

    # Always leave the worktree clean.
    _revert_fixture()

    summaries = [summarise(name, rows) for name, rows in all_rows.items()]

    md = format_markdown(summaries)
    (OUT_DIR / "rollup.md").write_text(md)

    # CSV
    fields = list(IncrementalSummary.__dataclass_fields__.keys())
    with (OUT_DIR / "rollup.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for s in summaries:
            w.writerow([asdict(s)[k] for k in fields])

    print("\n" + md)
    print(f"\nwrote {OUT_DIR}/rollup.md and rollup.csv")


if __name__ == "__main__":
    sys.exit(main())
