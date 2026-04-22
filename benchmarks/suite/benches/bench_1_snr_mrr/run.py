"""Bench #1 — Token-economy (SNR + MRR).

Re-scores the exact-symbol-lookup dataset (same queries as Bench #0) through
the token-cost lens: how much Acc@1 does the adapter deliver per 1K tokens
of response? This is THE defining MCP-agent metric — every tool response
eats context window.

The primary axis is `acc_at_1_per_kilo_token` = Acc@1 / (avg_tokens / 1000).
Bench #1 does NOT re-query adapters; it reads the committed jsonl output
from a Bench #0 run and re-scores.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from pathlib import Path

from benchmarks.suite.scoring import mrr as mrr_metric, latency_stats


BENCH_ID = "Bench #1 — Token Economy (SNR + MRR)"
PRIMARY_AXIS = "acc_at_1_per_kilo_token"
DATASET_VERSION = "fair-2026-04-20"  # same queries as Bench #0


@dataclass
class TokenEconomySummary:
    adapter: str
    n_queries: int
    acc_at_1_pct: float
    mrr: float
    avg_tokens: float
    tokens_per_hit: float            # total tokens across all queries / # of Acc@1 hits
    acc_at_1_per_kilo_token: float   # PRIMARY axis
    snr_pct: float                   # tokens-of-hits / total-tokens × 100
    avg_latency_ms: float


def rollup_from_jsonl(jsonl_path: Path) -> dict[str, TokenEconomySummary]:
    """Compute per-adapter token-economy summaries from a Bench #0 jsonl.

    Each row needs: `adapter, rank, tokens, latency_ms`. That's exactly
    what Bench #0's combined jsonl writes.
    """
    per_adapter: dict[str, list[dict]] = {}
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            per_adapter.setdefault(row["adapter"], []).append(row)

    out: dict[str, TokenEconomySummary] = {}
    for name, rows in per_adapter.items():
        n = len(rows)
        ranks = [r["rank"] for r in rows]
        tokens = [r["tokens"] for r in rows]
        hits_at_1 = sum(1 for r in ranks if r == 1)
        total_tokens = sum(tokens)
        # SNR: tokens from queries that actually found the answer at rank 1 /
        # total tokens. Interpretation: what fraction of the context window
        # the adapter spent on returning a correct top hit.
        tokens_on_hits = sum(tok for r, tok in zip(ranks, tokens) if r == 1)

        acc_at_1_pct = hits_at_1 / n * 100 if n else 0.0
        avg_tokens = total_tokens / n if n else 0.0
        tokens_per_hit = total_tokens / hits_at_1 if hits_at_1 else float("inf")
        # Primary axis: accuracy per 1K tokens (higher = better context economy)
        per_kilo = acc_at_1_pct / (avg_tokens / 1000) if avg_tokens else 0.0
        snr_pct = tokens_on_hits / total_tokens * 100 if total_tokens else 0.0
        lat = latency_stats([r["latency_ms"] for r in rows])

        out[name] = TokenEconomySummary(
            adapter=name,
            n_queries=n,
            acc_at_1_pct=round(acc_at_1_pct, 2),
            mrr=round(mrr_metric(ranks), 3),
            avg_tokens=round(avg_tokens, 0),
            tokens_per_hit=round(tokens_per_hit, 1) if tokens_per_hit != float("inf") else float("inf"),
            acc_at_1_per_kilo_token=round(per_kilo, 2),
            snr_pct=round(snr_pct, 2),
            avg_latency_ms=round(lat["mean"], 2),
        )
    return out


def format_markdown(rollup: dict[str, TokenEconomySummary]) -> str:
    lines = [
        f"# {BENCH_ID}",
        "",
        f"**Primary axis:** `{PRIMARY_AXIS}` (Acc@1 per 1,000 response tokens)",
        f"**Queries:** {next(iter(rollup.values())).n_queries if rollup else 0}",
        f"**Dataset version:** {DATASET_VERSION}",
        "",
        "| Adapter | Acc@1 | MRR | Avg tokens | Tokens/hit | **Acc@1/k-tok** | SNR | Avg latency |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in rollup.values():
        t_per_hit = f"{s.tokens_per_hit:.0f}" if s.tokens_per_hit != float("inf") else "∞"
        lines.append(
            f"| {s.adapter} | {s.acc_at_1_pct:.1f}% | {s.mrr:.3f} | "
            f"{int(round(s.avg_tokens))} | {t_per_hit} | "
            f"**{s.acc_at_1_per_kilo_token:.2f}** | {s.snr_pct:.1f}% | "
            f"{s.avg_latency_ms:.2f} ms |"
        )

    winner = max(rollup.values(), key=lambda s: s.acc_at_1_per_kilo_token)
    runners = [s for s in rollup.values() if s.adapter != winner.adapter]
    best_other = max(runners, key=lambda s: s.acc_at_1_per_kilo_token) if runners else None

    lines.extend([
        "",
        "## Primary axis result",
        "",
        (f"✅ **{winner.adapter} wins** `{PRIMARY_AXIS}` "
         f"({winner.acc_at_1_per_kilo_token:.2f} vs "
         f"{best_other.acc_at_1_per_kilo_token:.2f} — "
         f"{winner.acc_at_1_per_kilo_token / max(best_other.acc_at_1_per_kilo_token, 0.01):.1f}× lead)")
        if best_other else f"✅ **{winner.adapter} ran solo** — no comparison",
    ])
    return "\n".join(lines) + "\n"


def run_from_bench_0_jsonl(jsonl_path: Path, out_dir: Path) -> dict[str, TokenEconomySummary]:
    rollup = rollup_from_jsonl(jsonl_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    md = format_markdown(rollup)
    (out_dir / "rollup.md").write_text(md)

    # CSV
    import csv
    with (out_dir / "rollup.csv").open("w", newline="") as f:
        w = csv.writer(f)
        fields = list(TokenEconomySummary.__dataclass_fields__.keys())
        w.writerow(fields)
        for s in rollup.values():
            row = asdict(s)
            row["tokens_per_hit"] = (
                f"{row['tokens_per_hit']:.1f}"
                if row["tokens_per_hit"] != float("inf") else "inf"
            )
            w.writerow([row[k] for k in fields])

    return rollup
