"""jsonl → per-adapter rollup → markdown summary + CSV.

The rollup is a dict keyed by adapter name; each value is an AdapterSummary.
The primary-axis winner is determined mechanically: whichever adapter has the
best value on the declared `primary_axis` key of AdapterSummary.
"""
from __future__ import annotations
import csv
import json
import statistics
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

from benchmarks.suite.scoring import latency_stats


@dataclass
class AdapterSummary:
    adapter: str
    n_queries: int
    coverage_pct: float
    acc_at_1_pct: float
    acc_at_5_pct: float
    acc_at_10_pct: float
    mrr: float
    avg_latency_ms: float
    median_latency_ms: float
    p95_latency_ms: float
    avg_tokens: float


def _iter_rows(jsonl_path: Path) -> Iterator[dict]:
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def rollup_from_jsonl(jsonl_path: Path) -> dict[str, AdapterSummary]:
    per_adapter: dict[str, list[dict]] = {}
    for row in _iter_rows(jsonl_path):
        per_adapter.setdefault(row["adapter"], []).append(row)

    out: dict[str, AdapterSummary] = {}
    for name, rows in per_adapter.items():
        n = len(rows)
        lat_stats = latency_stats([r["latency_ms"] for r in rows])
        covered = sum(1 for r in rows if r["paths_count"] > 0)
        hit1 = sum(1 for r in rows if r["rank"] == 1)
        hit5 = sum(1 for r in rows if r["rank"] is not None and r["rank"] <= 5)
        hit10 = sum(1 for r in rows if r["rank"] is not None and r["rank"] <= 10)
        recips = [1.0 / r["rank"] if r["rank"] is not None else 0.0 for r in rows]
        tokens = [r["tokens"] for r in rows]

        out[name] = AdapterSummary(
            adapter=name,
            n_queries=n,
            coverage_pct=round(covered / n * 100, 2) if n else 0.0,
            acc_at_1_pct=round(hit1 / n * 100, 2) if n else 0.0,
            acc_at_5_pct=round(hit5 / n * 100, 2) if n else 0.0,
            acc_at_10_pct=round(hit10 / n * 100, 2) if n else 0.0,
            mrr=round(sum(recips) / n, 3) if n else 0.0,
            avg_latency_ms=round(lat_stats["mean"], 2),
            median_latency_ms=round(lat_stats["median"], 2),
            p95_latency_ms=round(lat_stats["p95"], 2),
            avg_tokens=round(statistics.mean(tokens), 0) if tokens else 0.0,
        )
    return out


def _primary_axis_value(s: AdapterSummary, axis: str) -> float:
    # Dotted access supported later (e.g., callers_of.recall); for Bench #0
    # the axis is always a direct attribute.
    return float(getattr(s, axis))


def format_markdown(
    rollup: dict[str, AdapterSummary],
    bench_id: str,
    primary_axis: str,
    dataset_version: str,
    n_queries: int,
) -> str:
    header = [
        f"# {bench_id}",
        "",
        f"**Primary axis:** `{primary_axis}`",
        f"**Queries:** {n_queries}",
        f"**Dataset version:** {dataset_version}",
        "",
        "| Adapter | Coverage | Acc@1 | Acc@5 | Acc@10 | MRR | Avg latency (ms) | Avg tokens |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    # Stable order: declared order of insertion into rollup preserved by dict.
    for s in rollup.values():
        header.append(
            f"| {s.adapter} | {s.coverage_pct:.1f}% | {s.acc_at_1_pct:.1f}% | "
            f"{s.acc_at_5_pct:.1f}% | {s.acc_at_10_pct:.1f}% | {s.mrr:.3f} | "
            f"{s.avg_latency_ms:.2f} | {int(round(s.avg_tokens))} |"
        )

    # Primary-axis winner.
    winner = max(rollup.values(), key=lambda s: _primary_axis_value(s, primary_axis))
    runners = [s for s in rollup.values() if s.adapter != winner.adapter]
    best_other = max(runners, key=lambda s: _primary_axis_value(s, primary_axis)) if runners else None
    winner_v = _primary_axis_value(winner, primary_axis)
    other_v = _primary_axis_value(best_other, primary_axis) if best_other else 0.0

    header.extend([
        "",
        "## Primary axis result",
        "",
        f"✅ **{winner.adapter} wins** `{primary_axis}` "
        f"({winner_v:.1f}% vs {other_v:.1f}%)."
        if primary_axis.endswith("_pct")
        else f"✅ **{winner.adapter} wins** `{primary_axis}` "
             f"({winner_v:.3f} vs {other_v:.3f}).",
    ])
    return "\n".join(header) + "\n"


def write_csv(rollup: dict[str, AdapterSummary], dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(list(AdapterSummary.__dataclass_fields__.keys()))
        for s in rollup.values():
            w.writerow(list(asdict(s).values()))
