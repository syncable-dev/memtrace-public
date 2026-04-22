"""Bench #4 — Incremental indexing & staleness.

Primary axis: `time_to_queryable.p95` ≤ 1000 ms.
Secondary: `staleness_rate`, incremental vs full-reindex throughput.

Ground truth: deterministic edit script over the scratch_fixture corpus
(see benchmarks/suite/datasets/bench_4_edits.json)."""
from .run import PRIMARY_AXIS, BENCH_ID, load_edits, run_with_adapter

__all__ = ["PRIMARY_AXIS", "BENCH_ID", "load_edits", "run_with_adapter"]
