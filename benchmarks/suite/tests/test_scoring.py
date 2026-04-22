import pytest
from benchmarks.suite.scoring import (
    acc_at_k, mrr, coverage, latency_stats, signal_to_noise,
    rank_of_first_hit,
    precision, recall, f1, time_to_queryable_p95, staleness_rate,
)


# Hits = list of (rank_or_None). rank 1-indexed, None = miss.

def test_rank_of_first_hit_finds_substring_match():
    # paths = ["a/x.py", "b/y.py", "c/z.py"]; expected = "b/y.py"
    # rank is 1-indexed -> 2
    assert rank_of_first_hit(["a/x.py", "b/y.py", "c/z.py"], "b/y.py") == 2


def test_rank_of_first_hit_no_match_returns_none():
    assert rank_of_first_hit(["a/x.py"], "nope.py") is None


def test_rank_of_first_hit_empty_paths():
    assert rank_of_first_hit([], "anything") is None


def test_acc_at_1():
    assert acc_at_k([1, 2, None, 1], k=1) == pytest.approx(0.5)  # 2/4


def test_acc_at_10_counts_all_hits_within_k():
    assert acc_at_k([1, 5, 10, 11, None], k=10) == pytest.approx(0.6)  # 3/5


def test_acc_at_k_empty():
    assert acc_at_k([], k=1) == 0.0


def test_mrr():
    # 1/1 + 1/2 + 0 + 1/4 = 1.75 / 4 = 0.4375
    assert mrr([1, 2, None, 4]) == pytest.approx(0.4375)


def test_mrr_all_miss():
    assert mrr([None, None]) == 0.0


def test_coverage():
    # any response at all (paths_count > 0)
    assert coverage([3, 0, 5, 0]) == pytest.approx(0.5)  # 2/4


def test_latency_stats_standard():
    s = latency_stats([10.0, 20.0, 30.0, 40.0, 50.0])
    assert s["mean"] == pytest.approx(30.0)
    assert s["median"] == pytest.approx(30.0)
    assert s["p50"] == pytest.approx(30.0)
    assert s["p95"] == pytest.approx(50.0)   # min(int(5*0.95)=4, 4) = 50.0
    assert s["p99"] == pytest.approx(50.0)


def test_latency_stats_empty_returns_zeros():
    s = latency_stats([])
    assert s == {"mean": 0.0, "median": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}


def test_signal_to_noise_perfect():
    # every chunk is from a correct source -> SNR = 1.0
    chunks = [("path/a.py", 100), ("path/a.py", 150)]
    assert signal_to_noise(chunks, gold_paths={"path/a.py"}) == pytest.approx(1.0)


def test_signal_to_noise_mixed():
    # 100 correct, 400 noise -> 100/500 = 0.2
    chunks = [("path/a.py", 100), ("other/b.py", 400)]
    assert signal_to_noise(chunks, gold_paths={"path/a.py"}) == pytest.approx(0.2)


def test_signal_to_noise_zero_tokens():
    assert signal_to_noise([], gold_paths={"x"}) == 0.0


# ── Bench #3 graph-query metrics ─────────────────────────────────────────────

def test_precision_perfect():
    # retrieved = gold -> 1.0
    assert precision({"a", "b"}, {"a", "b"}) == pytest.approx(1.0)


def test_precision_mixed():
    # 2 of 4 retrieved are correct -> 0.5
    assert precision({"a", "b", "x", "y"}, {"a", "b", "c"}) == pytest.approx(0.5)


def test_precision_empty_retrieved():
    # by definition 0.0 (cannot divide by zero; no signal returned)
    assert precision(set(), {"a"}) == 0.0


def test_precision_zero_intersection():
    assert precision({"x", "y"}, {"a", "b"}) == 0.0


def test_recall_perfect():
    assert recall({"a", "b"}, {"a", "b"}) == pytest.approx(1.0)


def test_recall_partial():
    # found 2 of 4 gold items -> 0.5
    assert recall({"a", "b"}, {"a", "b", "c", "d"}) == pytest.approx(0.5)


def test_recall_empty_gold():
    # undefined -> treat as 0.0 (bench config error, surfaces as row failure)
    assert recall({"a", "b"}, set()) == 0.0


def test_recall_zero_intersection():
    assert recall({"x"}, {"a", "b"}) == 0.0


def test_f1_balanced():
    # p=r=0.5 -> f1=0.5
    assert f1(0.5, 0.5) == pytest.approx(0.5)


def test_f1_perfect():
    assert f1(1.0, 1.0) == pytest.approx(1.0)


def test_f1_skewed():
    # p=1.0, r=0.25 -> 2 * 1 * 0.25 / 1.25 = 0.4
    assert f1(1.0, 0.25) == pytest.approx(0.4)


def test_f1_both_zero():
    assert f1(0.0, 0.0) == 0.0


# ── Bench #4 incremental-indexing metrics ────────────────────────────────────

def test_time_to_queryable_p95_standard():
    # 20 samples, p95 index = min(int(20*0.95)=19, 19) = index 19 -> 2000.0
    samples = [100.0 * i for i in range(1, 21)]  # 100..2000
    assert time_to_queryable_p95(samples) == pytest.approx(2000.0)


def test_time_to_queryable_p95_singleton():
    assert time_to_queryable_p95([500.0]) == pytest.approx(500.0)


def test_time_to_queryable_p95_empty_is_inf():
    assert time_to_queryable_p95([]) == float("inf")


def test_time_to_queryable_p95_unsorted_input():
    # must sort internally
    assert time_to_queryable_p95([50.0, 10.0, 30.0, 40.0, 20.0]) == pytest.approx(50.0)


def test_staleness_rate_zero():
    assert staleness_rate(0, 10) == 0.0


def test_staleness_rate_half():
    assert staleness_rate(5, 10) == pytest.approx(0.5)


def test_staleness_rate_empty_totals():
    # divide-by-zero guard: no queries → 0.0, not NaN
    assert staleness_rate(0, 0) == 0.0
