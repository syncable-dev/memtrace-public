"""Pure-function metrics. Benches compose these; they never invent primitives."""
from __future__ import annotations
import statistics


def rank_of_first_hit(paths: list[str], expected_file: str) -> int | None:
    """1-indexed rank of first path containing `expected_file` as substring
    (or vice versa, to match fair/ semantics). None on miss."""
    for i, p in enumerate(paths, start=1):
        if expected_file in p or p in expected_file:
            return i
    return None


def acc_at_k(ranks: list[int | None], k: int) -> float:
    """Fraction of queries whose correct result appeared in top-k."""
    if not ranks:
        return 0.0
    hit = sum(1 for r in ranks if r is not None and r <= k)
    return hit / len(ranks)


def mrr(ranks: list[int | None]) -> float:
    """Mean reciprocal rank. 1/rank on hit, 0 on miss."""
    if not ranks:
        return 0.0
    total = sum((1.0 / r) if r is not None else 0.0 for r in ranks)
    return total / len(ranks)


def coverage(paths_counts: list[int]) -> float:
    """Fraction of queries where the adapter returned any result at all."""
    if not paths_counts:
        return 0.0
    return sum(1 for c in paths_counts if c > 0) / len(paths_counts)


def latency_stats(ms: list[float]) -> dict[str, float]:
    """mean, median, p50, p95, p99 (all ms). Empty list -> all zeros."""
    if not ms:
        return {"mean": 0.0, "median": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
    srt = sorted(ms)
    def pct(q: float) -> float:
        return srt[min(int(len(srt) * q), len(srt) - 1)]
    return {
        "mean":   statistics.mean(srt),
        "median": statistics.median(srt),
        "p50":    pct(0.50),
        "p95":    pct(0.95),
        "p99":    pct(0.99),
    }


def signal_to_noise(
    chunks: list[tuple[str, int]],
    gold_paths: set[str],
) -> float:
    """SNR = tokens from correct sources / total tokens.

    `chunks` is a list of (source_path, token_count) pairs. `gold_paths` is
    the set of file paths considered the correct answer for this query.
    A chunk counts as "signal" iff its source path is a substring of some
    gold path or vice versa (same liberal match as rank_of_first_hit).
    """
    total = sum(tc for _, tc in chunks)
    if total == 0:
        return 0.0
    signal = 0
    for src, tc in chunks:
        for g in gold_paths:
            if src in g or g in src:
                signal += tc
                break
    return signal / total


# ── Bench #3 graph-query metrics ─────────────────────────────────────────────

def precision(retrieved: set[str], gold: set[str]) -> float:
    """|retrieved ∩ gold| / |retrieved|. 0.0 when retrieved is empty.

    Used by Bench #3: given the set of symbols a graph tool returned for
    callers_of/callees_of/impact_of, and the ground-truth set, what fraction
    of retrieved items are correct?
    """
    if not retrieved:
        return 0.0
    return len(retrieved & gold) / len(retrieved)


def recall(retrieved: set[str], gold: set[str]) -> float:
    """|retrieved ∩ gold| / |gold|. 0.0 when gold is empty.

    Bench #3 primary-axis component: what fraction of ground-truth callers
    did the adapter find?
    """
    if not gold:
        return 0.0
    return len(retrieved & gold) / len(gold)


def f1(p: float, r: float) -> float:
    """Harmonic mean of precision and recall. 0.0 when p + r == 0."""
    return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


# ── Bench #4 incremental-indexing metrics ────────────────────────────────────

def time_to_queryable_p95(deadlines_ms: list[float]) -> float:
    """p95 of observed "edit → queryable" latencies in ms.

    Bench #4 primary axis. Inputs are wall-times from edit-apply to the
    first successful adapter query against the new symbol state. An empty
    list returns inf (treated as a worst-case by the reporter).
    """
    if not deadlines_ms:
        return float("inf")
    srt = sorted(deadlines_ms)
    return srt[min(int(len(srt) * 0.95), len(srt) - 1)]


def staleness_rate(stale_hits: int, total_queries: int) -> float:
    """Fraction of queries that returned a pre-edit (stale) answer."""
    if total_queries == 0:
        return 0.0
    return stale_hits / total_queries
