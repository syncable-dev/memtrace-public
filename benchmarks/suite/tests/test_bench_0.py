from pathlib import Path
import pytest

from benchmarks.suite.benches.bench_0_exact_symbol import run as bench_0
from benchmarks.suite.contract import QueryResult
from benchmarks.suite.tests.fakes import FakeAdapter


def test_bench_0_declares_primary_axis():
    assert bench_0.PRIMARY_AXIS == "acc_at_1_pct"


def test_bench_0_declares_bench_id():
    assert bench_0.BENCH_ID == "Bench #0 — Exact-Symbol Lookup"


def test_bench_0_default_dataset_path_points_at_fair():
    p = bench_0.default_dataset_path()
    assert p.name == "dataset.json"
    assert p.parent.name == "fair"


def test_bench_0_run_with_fake_adapter(tmp_path):
    dataset = [
        {"id": "q1", "target_symbol": "foo", "expected_file": "a.py"},
        {"id": "q2", "target_symbol": "bar", "expected_file": "b.py"},
    ]
    canned = {
        "foo": QueryResult(paths=["a.py"], latency_ms=1.0, tokens_used=10),
        "bar": QueryResult(paths=["x.py"], latency_ms=2.0, tokens_used=20),
    }
    out_dir = tmp_path / "results"
    rows = bench_0.run_with_adapter(
        FakeAdapter(canned), dataset=dataset, out_dir=out_dir, limit=10,
    )
    assert len(rows) == 2
    # jsonl file written
    files = list(out_dir.glob("*.jsonl"))
    assert len(files) == 1
