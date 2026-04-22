"""Unit tests for Bench #1 — token-economy rollup from Bench #0 jsonl."""
import json
import pytest

from benchmarks.suite.benches.bench_1_snr_mrr.run import (
    BENCH_ID, PRIMARY_AXIS, rollup_from_jsonl, format_markdown,
)


def _write_jsonl(tmp_path, rows):
    p = tmp_path / "combined.jsonl"
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def test_bench_1_constants():
    assert BENCH_ID == "Bench #1 — Token Economy (SNR + MRR)"
    assert PRIMARY_AXIS == "acc_at_1_per_kilo_token"


def test_rollup_computes_acc_at_1_per_kilo_token(tmp_path):
    # 2 queries, 1 hit at rank 1; 100 tokens total → avg_tokens=50,
    # Acc@1=50%, per-kilo = 50 / (50/1000) = 1000.
    rows = [
        {"adapter": "A", "rank": 1, "tokens": 40, "latency_ms": 1.0},
        {"adapter": "A", "rank": None, "tokens": 60, "latency_ms": 2.0},
    ]
    rollup = rollup_from_jsonl(_write_jsonl(tmp_path, rows))
    a = rollup["A"]
    assert a.n_queries == 2
    assert a.acc_at_1_pct == 50.0
    assert a.avg_tokens == 50
    assert a.acc_at_1_per_kilo_token == pytest.approx(1000.0, abs=0.1)


def test_rollup_snr_is_hit_tokens_over_total(tmp_path):
    # Adapter A: 40 tokens on a hit, 60 on a miss. SNR = 40/100 = 40%.
    rows = [
        {"adapter": "A", "rank": 1, "tokens": 40, "latency_ms": 1.0},
        {"adapter": "A", "rank": None, "tokens": 60, "latency_ms": 2.0},
    ]
    rollup = rollup_from_jsonl(_write_jsonl(tmp_path, rows))
    assert rollup["A"].snr_pct == pytest.approx(40.0)


def test_rollup_mrr(tmp_path):
    # ranks: 1, 2, None → MRR = (1 + 0.5 + 0) / 3 = 0.5
    rows = [
        {"adapter": "A", "rank": 1, "tokens": 10, "latency_ms": 1.0},
        {"adapter": "A", "rank": 2, "tokens": 10, "latency_ms": 1.0},
        {"adapter": "A", "rank": None, "tokens": 10, "latency_ms": 1.0},
    ]
    rollup = rollup_from_jsonl(_write_jsonl(tmp_path, rows))
    assert rollup["A"].mrr == pytest.approx(0.5, abs=0.005)


def test_rollup_multi_adapter_preserves_order(tmp_path):
    rows = [
        {"adapter": "first",  "rank": 1,    "tokens": 100, "latency_ms": 1.0},
        {"adapter": "second", "rank": None, "tokens": 100, "latency_ms": 1.0},
        {"adapter": "first",  "rank": None, "tokens": 100, "latency_ms": 1.0},
    ]
    rollup = rollup_from_jsonl(_write_jsonl(tmp_path, rows))
    assert list(rollup.keys()) == ["first", "second"]


def test_format_markdown_names_primary_axis_winner(tmp_path):
    rows = [
        {"adapter": "winner", "rank": 1,    "tokens": 10,   "latency_ms": 1.0},
        {"adapter": "loser",  "rank": 1,    "tokens": 1000, "latency_ms": 1.0},
    ]
    rollup = rollup_from_jsonl(_write_jsonl(tmp_path, rows))
    md = format_markdown(rollup)
    assert "✅" in md
    assert "winner wins" in md
    assert "acc_at_1_per_kilo_token" in md


def test_format_markdown_single_adapter(tmp_path):
    rows = [{"adapter": "solo", "rank": 1, "tokens": 100, "latency_ms": 1.0}]
    md = format_markdown(rollup_from_jsonl(_write_jsonl(tmp_path, rows)))
    assert "ran solo" in md


def test_tokens_per_hit_handles_zero_hits(tmp_path):
    rows = [
        {"adapter": "A", "rank": None, "tokens": 50, "latency_ms": 1.0},
        {"adapter": "A", "rank": None, "tokens": 50, "latency_ms": 1.0},
    ]
    rollup = rollup_from_jsonl(_write_jsonl(tmp_path, rows))
    assert rollup["A"].tokens_per_hit == float("inf")
    assert rollup["A"].acc_at_1_pct == 0.0
