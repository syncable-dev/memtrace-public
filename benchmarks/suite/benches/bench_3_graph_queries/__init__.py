"""Bench #3 — Graph queries.

Primary axis: `callers_of.recall`.
Secondary: `callers_of.precision`, `impact_of.recall`, `find_dead_code.f1`,
latency.

Ground truth: pyright LSP call-hierarchy over the mempalace corpus
(see benchmarks/suite/datasets/bench_3_graph.json when present)."""
from .run import (
    BENCH_ID, PRIMARY_AXIS, DATASET_VERSION,
    load_dataset, run_with_adapter,
)

__all__ = [
    "BENCH_ID", "PRIMARY_AXIS", "DATASET_VERSION",
    "load_dataset", "run_with_adapter",
]
