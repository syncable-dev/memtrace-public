import pytest

from benchmarks.suite.contract import QueryResult
from benchmarks.suite.runner import run_bench_0, BenchRow
from benchmarks.suite.tests.fakes import FakeAdapter, CrashingAdapter


DATASET = [
    {"id": "q1", "target_symbol": "foo", "expected_file": "pkg/a.py"},
    {"id": "q2", "target_symbol": "bar", "expected_file": "pkg/b.py"},
    {"id": "q3", "target_symbol": "baz", "expected_file": "pkg/c.py"},
]


def test_runner_emits_one_row_per_query():
    canned = {
        "foo": QueryResult(paths=["pkg/a.py"], raw_response_text="ok", latency_ms=1.0, tokens_used=1),
        "bar": QueryResult(paths=["zzz/q.py", "pkg/b.py"], raw_response_text="ok", latency_ms=2.0, tokens_used=1),
        "baz": QueryResult(paths=[], raw_response_text="", latency_ms=3.0, tokens_used=0),
    }
    adapter = FakeAdapter(canned)
    rows = run_bench_0(adapter, DATASET, limit=10)
    assert len(rows) == 3


def test_runner_calls_setup_and_teardown():
    adapter = FakeAdapter({})
    run_bench_0(adapter, DATASET, limit=10)
    assert adapter.setup_called
    assert adapter.teardown_called


def test_runner_records_rank():
    canned = {
        "foo": QueryResult(paths=["pkg/a.py"]),          # rank 1
        "bar": QueryResult(paths=["zzz/q.py", "pkg/b.py"]),  # rank 2
        "baz": QueryResult(paths=[]),                    # miss
    }
    rows = run_bench_0(FakeAdapter(canned), DATASET, limit=10)
    by_id = {r.query_id: r for r in rows}
    assert by_id["q1"].rank == 1
    assert by_id["q2"].rank == 2
    assert by_id["q3"].rank is None


def test_runner_teardown_runs_even_on_exception():
    adapter = CrashingAdapter({})
    # runner must not raise; errors are recorded on the row
    rows = run_bench_0(adapter, DATASET[:1], limit=10)
    assert adapter.teardown_called
    assert rows[0].error is not None
    assert "boom" in rows[0].error


def test_runner_row_has_required_schema_fields():
    canned = {"foo": QueryResult(paths=["pkg/a.py"], latency_ms=1.5, tokens_used=7)}
    rows = run_bench_0(FakeAdapter(canned), DATASET[:1], limit=10)
    r = rows[0]
    assert r.query_id == "q1"
    assert r.target_symbol == "foo"
    assert r.expected_file == "pkg/a.py"
    assert r.paths_count == 1
    assert r.top_paths == ["pkg/a.py"]
    assert r.rank == 1
    assert r.latency_ms == pytest.approx(1.5)
    assert r.tokens == 7
    assert r.error is None


def test_runner_respects_per_query_limit_on_top_paths():
    canned = {"foo": QueryResult(paths=[f"pkg/{i}.py" for i in range(20)])}
    rows = run_bench_0(FakeAdapter(canned), DATASET[:1], limit=10)
    # top_paths kept for disk economy — capped at 3 regardless of `limit`
    assert len(rows[0].top_paths) == 3
