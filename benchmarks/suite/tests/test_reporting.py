from pathlib import Path
import pytest

from benchmarks.suite.reporting import (
    rollup_from_jsonl, AdapterSummary, format_markdown,
)

FIX = Path(__file__).parent / "fixtures"


def test_rollup_aggregates_per_adapter():
    rollup = rollup_from_jsonl(FIX / "canned_bench0_rows.jsonl")
    assert set(rollup.keys()) == {"memtrace", "chromadb"}
    mt = rollup["memtrace"]
    assert mt.n_queries == 5
    assert mt.acc_at_1_pct == pytest.approx(60.0)          # 3/5
    assert mt.acc_at_5_pct == pytest.approx(80.0)          # 4/5
    assert mt.coverage_pct == pytest.approx(80.0)          # 4/5 non-empty
    assert mt.mrr == pytest.approx(0.70, abs=0.005)        # (1+1+0.5+1+0)/5
    assert mt.avg_latency_ms == pytest.approx(9.20, abs=0.05)
    assert mt.avg_tokens == pytest.approx(157, abs=1)      # (195+180+210+200+0)/5


def test_rollup_chromadb_values():
    rollup = rollup_from_jsonl(FIX / "canned_bench0_rows.jsonl")
    cd = rollup["chromadb"]
    assert cd.acc_at_1_pct == pytest.approx(40.0)          # 2/5
    assert cd.coverage_pct == pytest.approx(100.0)
    assert cd.avg_tokens == pytest.approx(1930, abs=1)


def test_format_markdown_matches_golden():
    rollup = rollup_from_jsonl(FIX / "canned_bench0_rows.jsonl")
    got = format_markdown(rollup,
                          bench_id="Bench #0 — Exact-Symbol Lookup",
                          primary_axis="acc_at_1_pct",
                          dataset_version="test-fixture",
                          n_queries=5)
    expected = (FIX / "golden_bench0_rollup.md").read_text()
    assert got.rstrip() == expected.rstrip()


def test_primary_axis_winner():
    rollup = rollup_from_jsonl(FIX / "canned_bench0_rows.jsonl")
    # memtrace's Acc@1 (60) > chromadb's (40); memtrace must be named winner
    got = format_markdown(rollup, "Bench #0 — Exact-Symbol Lookup",
                          "acc_at_1_pct", "test-fixture", 5)
    assert "✅" in got
    assert "memtrace wins" in got
