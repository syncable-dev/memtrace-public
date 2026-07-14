#!/usr/bin/env python3
"""Failure-attribution triage for ContextBench runs.

Given the gold parquet, an adapter run's predictions.jsonl, the evaluator's
results.jsonl, and the per-instance AUDIT JSONs emitted by runner.py, classify
every task's dominant failure layer with numeric evidence and emit:

  triage.jsonl        one record per task
  triage_summary.json aggregates (per-layer counts/shares, per-language,
                      per-family, exclusion-pattern hits, next-lever ranking)
  triage_report.md    human-readable tables

Layer taxonomy (funnel order):

  INDEXING   The run produced nothing to grade: the instance hung / errored /
             emitted an empty prediction, or every search lane came back empty.
             Sub-causes: hang_or_empty_prediction, empty_search_space.
             NOTE: a gold file that never appears in any lane result and never
             reaches the candidate pool CANNOT be distinguished, from the audit
             alone, between "not indexed" and "indexed but never retrieved".
             Such files are attributed to RETRIEVAL; when their path matches a
             known exclusion pattern (e.g. pkg/, dist/, **/benchmarks/**) the
             task additionally gets an INDEXING secondary with the matched
             pattern as evidence (excluded-dir sub-cause).
  RETRIEVAL  Gold file never surfaced in any lane (searches, refinement
             searches, or graph contexts), surfaced only below the
             candidate-pool cutoff (best rank recorded), or surfaced only via
             graph context without reaching the pool (retrieval_graph_only —
             proves the file WAS indexed).
  SELECTION  Gold file reached the selector candidate pool (or the
             exact-identifier floor) but is absent from the final prediction.
             Includes gold that WAS in selected_candidate_ids but was dropped
             during final assembly (dropped_after_selection=true).
  BUDGET     The line budget structurally caps the score below the good
             threshold (cap_f1 = 2*min(g,b)/(g+min(g,b)) < threshold) AND at
             least one gold file made it into the final prediction — i.e. the
             pipeline engaged with gold and the budget is the binding
             constraint.
  SPAN       Right file(s) in the final prediction, wrong lines inside them.
  NONE       line F1 >= good threshold.

Triage runs strictly AFTER predictions exist; gold data is used only here,
never in retrieval.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LAYERS = ["INDEXING", "RETRIEVAL", "SELECTION", "BUDGET", "SPAN", "NONE"]

DEFAULT_LINE_BUDGET = 80
DEFAULT_GOOD_THRESHOLD = 0.5

# Fallback copy of runner.py's built-in exclusion policy, used when an audit
# JSON is unavailable for an instance (e.g. it hung before indexing finished).
FALLBACK_EXCLUDED_DIRS = [
    "node_modules", ".yarn", ".pnp", ".npm", ".pnpm", ".pnpm-store",
    "bower_components", ".bower_cache", "dist", "out", ".next", ".nuxt",
    ".svelte-kit", ".turbo", "storybook-static", ".expo", ".output",
    ".vercel", ".netlify", "vendor", "target", "bin", "obj", "pkg",
    "coverage", ".nyc_output", "htmlcov", "lcov-report", ".cache",
    "__pycache__", ".tox", ".venv", "venv", "env", ".env", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".hypothesis", ".eggs", ".idea",
    ".vscode", ".vs", ".fleet", ".terraform", ".gradle", ".m2", ".ivy2",
    "_site", "site", ".docusaurus", ".parcel-cache", ".rollup.cache",
    ".git", ".hg", ".svn", "npm", "skills", ".memdb", ".memtrace", ".claude",
]
FALLBACK_NOISE_PATTERNS = [
    "**/benchmarks/**", "**/benchmark/**", "**/bench/**", "**/perf/**",
    "**/perf_baselines/**", "**/datasets/**", "**/test_data/**",
    "**/tests/fixtures/**", "**/__fixtures__/**", "**/__test_data__/**",
]


# --------------------------------------------------------------------------
# Path handling
# --------------------------------------------------------------------------

_WORKSPACE_RE = re.compile(r"^/?workspace/[^/]+/(.*)$")
# Run-root layout emitted by runner.py: .../runs/<slug>/work/<slug>/repo/<rel>
_RUN_ROOT_RE = re.compile(r"^/.*?/work/[^/]+/repo/(.*)$")


def norm_path(path: Any) -> str:
    """Normalize gold/audit/prediction paths to a repo-relative form.

    Handles the three shapes seen in real artifacts:
      - gold parquet:   /workspace/<org>__<repo>__0.1/pkg/foo.go
      - audit lanes:    /<run-root>/.../work/<slug>/repo/astropy/foo.py
      - pool/final:     astropy/coordinates/foo.py (already relative)

    Order matters: the /workspace/<slug>/ form is matched FIRST, and the
    '/repo/' marker is only stripped from ABSOLUTE run-root paths (first
    occurrence). A legitimate repo-relative path containing a repo/ directory
    segment (e.g. gh cli's pkg/cmd/repo/list/list.go) is never mangled.
    """
    p = str(path or "").replace("\\", "/")
    m = _WORKSPACE_RE.match(p)
    if m:
        return m.group(1).lstrip("/")
    m = _RUN_ROOT_RE.match(p)
    if m:
        return m.group(1).lstrip("/")
    if p.startswith("/") and "/repo/" in p:
        # Absolute path in some other run layout: strip at the FIRST marker so
        # an in-repo repo/ directory further down the path survives.
        p = p.split("/repo/", 1)[1]
    return p.lstrip("/")


def exclusion_hit(
    rel_path: str, excluded_dirs: list[str], noise_patterns: list[str]
) -> str | None:
    """Return the matched exclusion pattern for a repo-relative path, if any."""
    segments = rel_path.split("/")[:-1]  # directory components only
    for seg in segments:
        if seg in excluded_dirs:
            return f"dir:{seg}/"
    for pat in noise_patterns:
        if fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch("/" + rel_path, pat):
            return f"noise:{pat}"
        stripped = pat[3:] if pat.startswith("**/") else pat
        if fnmatch.fnmatch(rel_path, stripped):
            return f"noise:{pat}"
    return None


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_gold(gold_parquet: Path, instance_ids: set[str] | None = None) -> dict[str, dict[str, Any]]:
    import pandas as pd

    df = pd.read_parquet(gold_parquet)
    gold: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        iid = str(row["instance_id"])
        if instance_ids is not None and iid not in instance_ids:
            continue
        raw = row["gold_context"]
        spans = json.loads(raw) if isinstance(raw, str) else raw
        per_file: dict[str, set[int]] = defaultdict(set)
        for span in spans:
            f = norm_path(span["file"])
            per_file[f].update(range(int(span["start_line"]), int(span["end_line"]) + 1))
        gold[iid] = {
            "repo": str(row.get("repo", "")),
            "language": str(row.get("language", "")),
            "files": {f: len(lines) for f, lines in per_file.items()},
        }
    return gold


def load_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out[str(row["instance_id"])] = row
    return out


def find_audit(audit_dir: Path, instance_id: str) -> Path | None:
    """Resolve an instance's audit JSON in either supported layout:
    flat (<audit_dir>/<iid>.json) or runs (<audit_dir>/<iid>/prediction-audit/<iid>.json).
    """
    candidates = [
        audit_dir / f"{instance_id}.json",
        audit_dir / instance_id / "prediction-audit" / f"{instance_id}.json",
        audit_dir / instance_id / f"{instance_id}.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def discover_instances(audit_dir: Path) -> set[str]:
    """Instances visible in the audit dir, including ones that hung (run dir
    present but no audit JSON written)."""
    ids: set[str] = set()
    if not audit_dir.is_dir():
        return ids
    for entry in audit_dir.iterdir():
        if entry.is_file() and entry.suffix == ".json":
            ids.add(entry.stem)
        elif entry.is_dir():
            ids.add(entry.name)
    return ids


# --------------------------------------------------------------------------
# Funnel extraction from one audit JSON
# --------------------------------------------------------------------------

@dataclass
class GoldFileStatus:
    file: str
    gold_lines: int
    best_rank: int | None = None
    best_lane: str | None = None
    in_primary_search: bool = False
    in_refinement: bool = False
    in_exact_identifier: bool = False
    in_graph_context: bool = False
    in_pool: bool = False
    in_selected: bool = False
    in_final_prediction: bool = False
    excluded_pattern: str | None = None

    @property
    def surfaced(self) -> bool:
        return (
            self.best_rank is not None
            or self.in_exact_identifier
            or self.in_graph_context
        )

    def bucket(self) -> str:
        if self.in_final_prediction:
            return "final"
        if self.in_pool or self.in_exact_identifier:
            return "selection"
        if self.best_rank is not None:
            return "retrieval_below_pool"
        if self.in_graph_context:
            # Surfaced via graph expansion (so it WAS indexed and retrieved by
            # a real lane feeding the selector pool) but never made the pool.
            return "retrieval_graph_only"
        return "retrieval_missing"


@dataclass
class Funnel:
    statuses: list[GoldFileStatus] = field(default_factory=list)
    audit_found: bool = False
    search_space_empty: bool = False
    excluded_dirs: list[str] = field(default_factory=lambda: list(FALLBACK_EXCLUDED_DIRS))
    noise_patterns: list[str] = field(default_factory=lambda: list(FALLBACK_NOISE_PATTERNS))


def collect_context_files(obj: Any, acc: set[str]) -> None:
    """Recursively collect every file/file_path string in a nested structure
    (used for graph_contexts, whose anchors and expansions nest arbitrarily)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("file", "file_path") and isinstance(v, str):
                acc.add(norm_path(v))
            else:
                collect_context_files(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            collect_context_files(v, acc)


def build_funnel(
    audit: dict[str, Any] | None,
    gold_files: dict[str, int],
    predicted_files: set[str],
) -> Funnel:
    funnel = Funnel(audit_found=audit is not None)
    audit = audit or {}

    policy = (audit.get("index_result") or {}).get("exclusion_policy") or {}
    if policy.get("default_excluded_dirs"):
        funnel.excluded_dirs = [str(d) for d in policy["default_excluded_dirs"]]
    if policy.get("default_noise_patterns"):
        funnel.noise_patterns = [str(p) for p in policy["default_noise_patterns"]]

    # Rank per file across lanes.
    lane_best: dict[str, tuple[int, str]] = {}
    total_results = 0
    for lane_name in ("searches", "refinement_searches"):
        for search in audit.get(lane_name) or []:
            for rank, result in enumerate(search.get("results") or [], start=1):
                total_results += 1
                f = norm_path(result.get("file_path") or result.get("file") or "")
                prev = lane_best.get(f)
                if prev is None or rank < prev[0]:
                    lane_best[f] = (rank, lane_name)

    exact_files = {
        norm_path(c.get("file") or "")
        for c in audit.get("exact_identifier_candidates") or []
    }

    # Third retrieval surface: graph expansion. Files here provably reached
    # the selector pool on real runs, and their presence proves they WERE
    # indexed — never conflate them with "never surfaced anywhere".
    graph_files: set[str] = set()
    collect_context_files(audit.get("graph_contexts"), graph_files)

    selector = audit.get("selector") or {}
    pool = selector.get("candidate_pool") or []
    selected_ids = set(selector.get("selected_candidate_ids") or [])
    pool_files = {norm_path(c.get("file") or "") for c in pool}
    selected_files = {
        norm_path(c.get("file") or "") for c in pool if c.get("id") in selected_ids
    }

    final_files = set(predicted_files)
    if not final_files:
        final_files = {norm_path(c.get("file") or "") for c in audit.get("final_candidates") or []}

    funnel.search_space_empty = bool(audit) and total_results == 0 and not pool

    for f, n_lines in sorted(gold_files.items(), key=lambda kv: -kv[1]):
        status = GoldFileStatus(file=f, gold_lines=n_lines)
        hit = lane_best.get(f)
        if hit:
            status.best_rank, lane = hit
            status.best_lane = lane
            status.in_primary_search = any(
                norm_path(r.get("file_path") or r.get("file") or "") == f
                for s in audit.get("searches") or []
                for r in s.get("results") or []
            )
            status.in_refinement = any(
                norm_path(r.get("file_path") or r.get("file") or "") == f
                for s in audit.get("refinement_searches") or []
                for r in s.get("results") or []
            )
        status.in_exact_identifier = f in exact_files
        status.in_graph_context = f in graph_files
        if status.best_lane is None and status.in_graph_context:
            status.best_lane = "graph_contexts"
        status.in_pool = f in pool_files
        status.in_selected = f in selected_files
        status.in_final_prediction = f in final_files
        status.excluded_pattern = exclusion_hit(f, funnel.excluded_dirs, funnel.noise_patterns)
        funnel.statuses.append(status)
    return funnel


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def f1(recall: float, precision: float) -> float:
    if recall + precision <= 0:
        return 0.0
    return 2 * recall * precision / (recall + precision)


def extract_metrics(result_row: dict[str, Any] | None) -> dict[str, dict[str, float]]:
    metrics: dict[str, dict[str, float]] = {}
    final = (result_row or {}).get("final") or {}
    for family in ("file", "symbol", "span", "line"):
        m = final.get(family) or {}
        recall = float(m.get("coverage", 0.0))
        precision = float(m.get("precision", 0.0))
        metrics[family] = {
            "recall": recall,
            "precision": precision,
            "f1": round(f1(recall, precision), 4),
            "gold_size": m.get("gold_size"),
            "pred_size": m.get("pred_size"),
        }
    return metrics


def cap_f1_at_budget(gold_lines: int, budget: int) -> float:
    """Best achievable line F1 with a hard prediction budget of `budget` lines
    (perfect precision, recall = min(gold, budget) / gold)."""
    if gold_lines <= 0:
        return 1.0
    reachable = min(gold_lines, budget)
    return 2 * reachable / (gold_lines + reachable)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def classify(
    instance_id: str,
    gold_info: dict[str, Any],
    funnel: Funnel,
    metrics: dict[str, dict[str, float]],
    has_prediction: bool,
    budget: int,
    good_threshold: float,
) -> dict[str, Any]:
    parts = instance_id.split("__")
    family = parts[0] if parts else ""
    language = parts[1] if len(parts) > 1 else gold_info.get("language", "")

    gold_files = gold_info["files"]
    total_gold = int(metrics["line"]["gold_size"] or 0) or sum(gold_files.values())
    line_f1 = metrics["line"]["f1"]
    line_recall = metrics["line"]["recall"]
    file_f1 = metrics["file"]["f1"]
    cap = round(cap_f1_at_budget(total_gold, budget), 4)

    # Lost-line buckets from the funnel.
    buckets = {"retrieval": 0, "selection": 0, "final": 0}
    excluded_lines = 0
    excluded_patterns: list[str] = []
    for st in funnel.statuses:
        b = st.bucket()
        if b in ("retrieval_missing", "retrieval_below_pool", "retrieval_graph_only"):
            buckets["retrieval"] += st.gold_lines
        elif b == "selection":
            buckets["selection"] += st.gold_lines
        else:
            buckets["final"] += st.gold_lines
        # A graph-context appearance proves the file WAS indexed, so only
        # never-surfaced-anywhere files can carry the excluded-dir evidence.
        if b == "retrieval_missing" and st.excluded_pattern:
            excluded_lines += st.gold_lines
            excluded_patterns.append(st.excluded_pattern)

    # Approximate lines missed INSIDE files that made the final prediction.
    achieved_gold_lines = round(line_recall * total_gold)
    span_missed = max(0, buckets["final"] - achieved_gold_lines)

    dropped_after_selection = any(
        st.in_selected and not st.in_final_prediction for st in funnel.statuses
    )

    primary = secondary = None
    sub_cause = None
    explanation = ""

    def loss_ranking() -> list[tuple[str, int]]:
        losses = [
            ("RETRIEVAL", buckets["retrieval"]),
            ("SELECTION", buckets["selection"]),
            ("SPAN", span_missed),
        ]
        return sorted([x for x in losses if x[1] > 0], key=lambda x: -x[1])

    if line_f1 >= good_threshold:
        primary = "NONE"
        explanation = f"line F1 {line_f1:.3f} >= good threshold {good_threshold}"
    elif not has_prediction or (not funnel.audit_found and not has_prediction):
        primary = "INDEXING"
        sub_cause = "hang_or_empty_prediction"
        explanation = (
            "no prediction was produced (run hung, errored, or emitted nothing); "
            f"audit JSON {'present' if funnel.audit_found else 'missing'}"
        )
    elif funnel.search_space_empty:
        primary = "INDEXING"
        sub_cause = "empty_search_space"
        explanation = "audit shows zero results across all search lanes and an empty candidate pool"
    elif buckets["final"] > 0 and cap < good_threshold:
        primary = "BUDGET"
        ratio = line_f1 / cap if cap > 0 else 0.0
        explanation = (
            f"gold ({total_gold} lines) exceeds the {budget}-line budget: cap F1 {cap:.3f} "
            f"< {good_threshold}; achieved {line_f1:.3f} ({ratio:.0%} of cap) with "
            f"{buckets['final']}/{total_gold} gold lines in selected files"
        )
        ranking = loss_ranking()
        if ranking:
            secondary = ranking[0][0]
            explanation += f"; largest residual loss: {secondary} ({ranking[0][1]} gold lines)"
    else:
        ranking = loss_ranking()
        if not ranking:
            primary = "SPAN"
            explanation = (
                f"gold files fully in final prediction but line F1 {line_f1:.3f} low "
                f"(file F1 {file_f1:.3f})"
            )
        else:
            primary = ranking[0][0]
            if len(ranking) > 1:
                secondary = ranking[1][0]
            if primary == "RETRIEVAL":
                missing = [s for s in funnel.statuses if s.bucket() == "retrieval_missing"]
                below = [s for s in funnel.statuses if s.bucket() == "retrieval_below_pool"]
                graph_only = [
                    s for s in funnel.statuses if s.bucket() == "retrieval_graph_only"
                ]
                bits = []
                if missing:
                    bits.append(
                        f"{len(missing)} gold file(s) ({sum(s.gold_lines for s in missing)} lines) "
                        "never surfaced in any lane (searches, refinement, graph)"
                    )
                if below:
                    ranks = ", ".join(f"{s.file}@{s.best_rank}" for s in below)
                    bits.append(f"{len(below)} surfaced below pool cutoff ({ranks})")
                if graph_only:
                    bits.append(
                        f"{len(graph_only)} gold file(s) "
                        f"({sum(s.gold_lines for s in graph_only)} lines) surfaced only "
                        "via graph context (indexed, but absent from search lanes and pool)"
                    )
                explanation = "; ".join(bits)
            elif primary == "SELECTION":
                pool_files = [s for s in funnel.statuses if s.bucket() == "selection"]
                n_sel = sum(1 for s in funnel.statuses if s.in_selected)
                explanation = (
                    f"{len(pool_files)}/{len(funnel.statuses)} gold file(s) "
                    f"({buckets['selection']} lines) in selector pool but absent from final "
                    f"prediction (selector picked {n_sel} gold file(s))"
                )
                if dropped_after_selection:
                    explanation += "; gold WAS selected but dropped during final assembly"
                    sub_cause = "dropped_after_selection"
            else:
                explanation = (
                    f"right file(s) selected ({buckets['final']}/{total_gold} gold lines in "
                    f"final files) but only ~{achieved_gold_lines} gold lines hit "
                    f"(file F1 {file_f1:.3f}, line F1 {line_f1:.3f})"
                )
        # Exclusion-pattern evidence upgrades/sets an INDEXING secondary.
        if excluded_lines > 0 and primary != "INDEXING":
            secondary = "INDEXING"
            sub_cause = f"excluded-dir:{sorted(set(excluded_patterns))[0]}"
            explanation += (
                f"; {excluded_lines} gold lines live under exclusion pattern(s) "
                f"{sorted(set(excluded_patterns))} -> likely never indexed"
            )

    return {
        "instance_id": instance_id,
        "family": family,
        "language": language,
        "repo": gold_info.get("repo", ""),
        "primary": primary,
        "secondary": secondary,
        "sub_cause": sub_cause,
        "explanation": explanation,
        "gold_total_lines": total_gold,
        "gold_files": [
            {
                "file": st.file,
                "gold_lines": st.gold_lines,
                "bucket": st.bucket(),
                "best_rank": st.best_rank,
                "best_lane": st.best_lane,
                "in_primary_search": st.in_primary_search,
                "in_refinement": st.in_refinement,
                "in_exact_identifier": st.in_exact_identifier,
                "in_graph_context": st.in_graph_context,
                "in_pool": st.in_pool,
                "in_selected": st.in_selected,
                "in_final_prediction": st.in_final_prediction,
                "excluded_pattern": st.excluded_pattern,
            }
            for st in funnel.statuses
        ],
        "buckets_gold_lines": {
            "retrieval": buckets["retrieval"],
            "selection": buckets["selection"],
            "in_final_files": buckets["final"],
            "span_missed_estimate": span_missed,
            "excluded_pattern_lines": excluded_lines,
        },
        "budget": {
            "line_budget": budget,
            "cap_f1_at_budget": cap,
            "achieved_over_cap": round(line_f1 / cap, 4) if cap > 0 else None,
            "budget_capped": cap < good_threshold,
        },
        "metrics": metrics,
        "audit_found": funnel.audit_found,
        "has_prediction": has_prediction,
    }


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def aggregate(records: list[dict[str, Any]], good_threshold: float) -> dict[str, Any]:
    n = len(records)

    def layer_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        counts = {layer: 0 for layer in LAYERS}
        for r in rows:
            counts[r["primary"]] += 1
        return {k: v for k, v in counts.items() if v}

    by_language: dict[str, dict[str, int]] = {}
    by_family: dict[str, dict[str, int]] = {}
    for key, split in (("language", by_language), ("family", by_family)):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in records:
            groups[r[key] or "unknown"].append(r)
        for g, rows in sorted(groups.items()):
            split[g] = layer_counts(rows)

    exclusion_hits: dict[str, int] = defaultdict(int)
    for r in records:
        for gf in r["gold_files"]:
            if gf["excluded_pattern"] and gf["bucket"] == "retrieval_missing":
                exclusion_hits[gf["excluded_pattern"]] += 1

    # Next-lever ranking: if layer L were fixed on tasks where it is primary,
    # each such task could plausibly reach its budget cap. For BUDGET the
    # lever is raising the budget; its booked headroom is BOUNDED by how much
    # of the current cap the task actually achieves (a task hitting 0% of its
    # cap gains ~nothing from a bigger budget alone). Residual (cap - f1)
    # losses on BUDGET-primary tasks are additionally credited to the task's
    # secondary layer as residual_within_cap_sum, so actionable levers are not
    # starved whenever BUDGET dominates.
    levers = []
    for layer in ("INDEXING", "RETRIEVAL", "SELECTION", "BUDGET", "SPAN"):
        prim = [r for r in records if r["primary"] == layer]
        sec = [r for r in records if r["secondary"] == layer]
        if not prim and not sec:
            continue
        if layer == "BUDGET":
            headroom = 0.0
            for r in prim:
                cap = r["budget"]["cap_f1_at_budget"]
                ratio = min(1.0, r["metrics"]["line"]["f1"] / cap) if cap > 0 else 0.0
                headroom += ratio * (1.0 - cap)
            residual = 0.0
        else:
            headroom = sum(
                max(0.0, r["budget"]["cap_f1_at_budget"] - r["metrics"]["line"]["f1"])
                for r in prim
            )
            residual = sum(
                max(0.0, r["budget"]["cap_f1_at_budget"] - r["metrics"]["line"]["f1"])
                for r in records
                if r["primary"] == "BUDGET" and r["secondary"] == layer
            )
        lever: dict[str, Any] = {
            "layer": layer,
            "tasks_primary": len(prim),
            "tasks_secondary": len(sec),
            "est_line_f1_headroom_sum": round(headroom, 4),
            "residual_within_cap_sum": round(residual, 4),
            "est_macro_line_f1_gain": round((headroom + residual) / n, 4) if n else 0.0,
            "instances_primary": [r["instance_id"] for r in prim],
        }
        if layer == "BUDGET":
            lever["note"] = (
                "lever = raising the line budget, which the benchmark fixes; "
                "headroom bounded by achieved/cap ratio, listed for accounting only"
            )
        levers.append(lever)
    levers.sort(
        key=lambda l: -(l["est_line_f1_headroom_sum"] + l["residual_within_cap_sum"])
    )

    macro_line_f1 = sum(r["metrics"]["line"]["f1"] for r in records) / n if n else 0.0
    macro_cap = sum(r["budget"]["cap_f1_at_budget"] for r in records) / n if n else 0.0

    counts = layer_counts(records)
    return {
        "num_tasks": n,
        "good_threshold": good_threshold,
        "macro_line_f1": round(macro_line_f1, 4),
        "macro_cap_f1_at_budget": round(macro_cap, 4),
        "layer_counts": counts,
        "layer_share": {k: round(v / n, 4) for k, v in counts.items()} if n else {},
        "by_language": by_language,
        "by_family": by_family,
        "exclusion_pattern_hits": dict(
            sorted(exclusion_hits.items(), key=lambda kv: -kv[1])
        ),
        "next_lever_ranking": levers,
    }


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def render_report(records: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# ContextBench failure triage")
    lines.append("")
    lines.append(
        f"{summary['num_tasks']} tasks | macro line F1 {summary['macro_line_f1']:.3f} | "
        f"macro cap@budget {summary['macro_cap_f1_at_budget']:.3f} | "
        f"good threshold {summary['good_threshold']}"
    )
    lines.append("")
    lines.append(
        "Denominator policy: every discovered instance is triaged, INCLUDING "
        "hung/watchdog-killed ones (scored as line F1 0). With the same manifest, "
        "this macro should match report.py's manifest-complete line-F1 macro."
    )
    bc = summary.get("budget_check") or {}
    if bc and not bc.get("ok", True):
        lines.append("")
        lines.append(
            f"**WARNING: --line-budget {bc.get('line_budget')} does not match this "
            f"run** (max unique predicted lines {bc.get('max_unique_predicted_lines')}; "
            f"instances with line F1 > cap: "
            f"{', '.join(bc.get('impossible_achieved_over_cap_instances') or []) or 'none'}). "
            "BUDGET attribution and cap columns below are unreliable — re-run with "
            "the run's actual budget."
        )
    if summary.get("untriaged_instances"):
        lines.append("")
        lines.append(
            f"WARNING: {len(summary['untriaged_instances'])} discovered instance(s) "
            f"missing from the gold parquet were NOT triaged: "
            f"{', '.join(summary['untriaged_instances'])}"
        )
    lines.append("")
    lines.append("## Layer shares")
    lines.append("")
    lines.append("| layer | tasks | share |")
    lines.append("|---|---|---|")
    for layer in LAYERS:
        if layer in summary["layer_counts"]:
            lines.append(
                f"| {layer} | {summary['layer_counts'][layer]} | "
                f"{summary['layer_share'][layer]:.0%} |"
            )
    lines.append("")
    lines.append("## Next-lever ranking")
    lines.append("")
    lines.append(
        "| layer | primary tasks | secondary tasks | est. line-F1 headroom (sum) | "
        "residual within cap (sum) | est. macro gain |"
    )
    lines.append("|---|---|---|---|---|---|")
    for lever in summary["next_lever_ranking"]:
        lines.append(
            f"| {lever['layer']} | {lever['tasks_primary']} | {lever['tasks_secondary']} | "
            f"{lever['est_line_f1_headroom_sum']:.3f} | "
            f"{lever['residual_within_cap_sum']:.3f} | {lever['est_macro_line_f1_gain']:.3f} |"
        )
    lines.append("")
    lines.append("## By language")
    lines.append("")
    lines.append("| language | " + " | ".join(LAYERS) + " |")
    lines.append("|---" * (len(LAYERS) + 1) + "|")
    for lang, counts in summary["by_language"].items():
        lines.append(
            f"| {lang} | " + " | ".join(str(counts.get(l, "")) for l in LAYERS) + " |"
        )
    lines.append("")
    lines.append("## By dataset family")
    lines.append("")
    lines.append("| family | " + " | ".join(LAYERS) + " |")
    lines.append("|---" * (len(LAYERS) + 1) + "|")
    for fam, counts in summary["by_family"].items():
        lines.append(
            f"| {fam} | " + " | ".join(str(counts.get(l, "")) for l in LAYERS) + " |"
        )
    lines.append("")
    if summary["exclusion_pattern_hits"]:
        lines.append("## Exclusion-pattern hits (gold files never surfaced)")
        lines.append("")
        lines.append("| pattern | gold files hit |")
        lines.append("|---|---|")
        for pat, cnt in summary["exclusion_pattern_hits"].items():
            lines.append(f"| `{pat}` | {cnt} |")
        lines.append("")
    lines.append("## Per-task triage")
    lines.append("")
    lines.append("| instance | lang | primary | secondary | line F1 | cap@budget | explanation |")
    lines.append("|---|---|---|---|---|---|---|")
    order = {layer: i for i, layer in enumerate(LAYERS)}
    for r in sorted(records, key=lambda r: (order[r["primary"]], r["instance_id"])):
        short = r["instance_id"].split("__")[-1]
        repo = r["repo"].split("/")[-1] or short
        lines.append(
            f"| {repo} `{short}` | {r['language']} | **{r['primary']}** | "
            f"{r['secondary'] or ''} | {r['metrics']['line']['f1']:.3f} | "
            f"{r['budget']['cap_f1_at_budget']:.3f} | {r['explanation']} |"
        )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def load_manifest_ids(manifest_path: Path) -> set[str]:
    """Instance ids from a run/slice manifest: a JSON list of ids, a list of
    {"instance_id": ...} dicts, or a dict with a "tasks" key of either form."""
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = loaded.get("tasks") if isinstance(loaded, dict) else loaded
    ids: set[str] = set()
    for item in items or []:
        if isinstance(item, dict):
            iid = item.get("instance_id")
            if iid:
                ids.add(str(iid))
        else:
            ids.add(str(item))
    return ids


def max_unique_predicted_lines(predictions: dict[str, dict[str, Any]]) -> int | None:
    """Max, over instances, of the number of unique (file, line) pairs in the
    final prediction — a lower bound on the run's actual line budget."""
    best: int | None = None
    for row in predictions.values():
        spans = (row.get("traj_data") or {}).get("pred_spans")
        lines = 0
        try:
            if isinstance(spans, dict):  # {file: [{start, end}, ...]}
                for file_spans in spans.values():
                    covered: set[int] = set()
                    for s in file_spans or []:
                        covered.update(
                            range(int(s.get("start", s.get("start_line", 0))),
                                  int(s.get("end", s.get("end_line", 0))) + 1)
                        )
                    lines += len(covered)
            elif isinstance(spans, list):  # [{file, start_line, end_line}, ...]
                per_file: dict[str, set[int]] = defaultdict(set)
                for s in spans:
                    per_file[str(s.get("file"))].update(
                        range(int(s.get("start_line", 0)), int(s.get("end_line", 0)) + 1)
                    )
                lines = sum(len(v) for v in per_file.values())
        except (TypeError, ValueError, AttributeError):
            continue
        if lines and (best is None or lines > best):
            best = lines
    return best


def budget_sanity_check(
    records: list[dict[str, Any]], budget: int, max_pred_lines: int | None
) -> dict[str, Any]:
    """Detect a --line-budget that contradicts the run itself. Line F1 can
    NEVER exceed cap_f1_at_budget when the budget is right, and no prediction
    can contain more unique lines than the budget."""
    impossible = sorted(
        r["instance_id"]
        for r in records
        if r["budget"]["cap_f1_at_budget"] is not None
        and r["metrics"]["line"]["f1"] > r["budget"]["cap_f1_at_budget"] + 5e-4
    )
    over_budget = max_pred_lines is not None and max_pred_lines > budget
    return {
        "line_budget": budget,
        "max_unique_predicted_lines": max_pred_lines,
        "impossible_achieved_over_cap_instances": impossible,
        "ok": not impossible and not over_budget,
    }


def run_triage(
    gold_parquet: Path,
    predictions_path: Path,
    results_path: Path,
    audit_dir: Path,
    budget: int = DEFAULT_LINE_BUDGET,
    good_threshold: float = DEFAULT_GOOD_THRESHOLD,
    manifest_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    predictions = load_jsonl(predictions_path)
    results = load_jsonl(results_path)
    universe = set(predictions) | set(results) | discover_instances(audit_dir)
    if manifest_path is not None:
        # The manifest is the authoritative task universe: instances that hung
        # or were watchdog-killed before writing ANY artifact still get triaged
        # (as INDEXING) instead of silently shrinking the denominator.
        universe |= load_manifest_ids(manifest_path)
    gold = load_gold(gold_parquet, universe)
    instance_ids = sorted(universe & set(gold))
    untriaged = sorted(universe - set(gold))
    if universe and not instance_ids:
        raise SystemExit(
            f"ERROR: none of the {len(universe)} discovered instances appear in "
            f"{gold_parquet} — wrong --gold?"
        )

    records: list[dict[str, Any]] = []
    for iid in instance_ids:
        audit_path = find_audit(audit_dir, iid)
        audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path else None

        predicted_files: set[str] = set()
        pred_row = predictions.get(iid)
        has_prediction = False
        if pred_row:
            traj = pred_row.get("traj_data") or {}
            # The evaluator scores traj_data.pred_files / pred_spans (the final
            # prediction); pred_steps is only the exploration trajectory.
            final_files = traj.get("pred_files")
            if not final_files:
                steps = traj.get("pred_steps") or []
                final_files = steps[-1].get("files") if steps else []
            predicted_files = {norm_path(f) for f in final_files or []}
            has_prediction = bool(predicted_files)

        funnel = build_funnel(audit, gold[iid]["files"], predicted_files)
        metrics = extract_metrics(results.get(iid))
        record = classify(
            iid, gold[iid], funnel, metrics, has_prediction, budget, good_threshold
        )
        # Watchdog-killed instances (runs/ layout marker written by
        # aws/remote/run-remote.sh) get a distinct INDEXING sub-cause.
        if (
            record["primary"] == "INDEXING"
            and (audit_dir / iid / "WATCHDOG_TIMEOUT").is_file()
        ):
            record["sub_cause"] = "watchdog_timeout"
            record["explanation"] += (
                "; WATCHDOG_TIMEOUT marker present (killed by the per-instance watchdog)"
            )
        records.append(record)

    summary = aggregate(records, good_threshold)
    summary["untriaged_instances"] = untriaged
    summary["budget_check"] = budget_sanity_check(
        records, budget, max_unique_predicted_lines(predictions)
    )
    return records, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gold", required=True, type=Path, help="gold parquet (post-hoc only)")
    parser.add_argument("--predictions", required=True, type=Path, help="predictions.jsonl")
    parser.add_argument("--results", required=True, type=Path, help="evaluator results.jsonl")
    parser.add_argument(
        "--audit-dir",
        required=True,
        type=Path,
        help="predictions-audit/ (flat <iid>.json) or runs/ (<iid>/prediction-audit/<iid>.json)",
    )
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--line-budget", type=int, default=DEFAULT_LINE_BUDGET)
    parser.add_argument("--good-threshold", type=float, default=DEFAULT_GOOD_THRESHOLD)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="run/slice manifest JSON — makes its instance ids the authoritative "
        "task universe so instances that died before writing any artifact are "
        "still triaged (as INDEXING)",
    )
    args = parser.parse_args()

    records, summary = run_triage(
        args.gold,
        args.predictions,
        args.results,
        args.audit_dir,
        budget=args.line_budget,
        good_threshold=args.good_threshold,
        manifest_path=args.manifest,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "triage.jsonl").open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    (args.out_dir / "triage_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    (args.out_dir / "triage_report.md").write_text(
        render_report(records, summary), encoding="utf-8"
    )

    print(f"triaged {len(records)} tasks -> {args.out_dir}")
    for layer, cnt in summary["layer_counts"].items():
        print(f"  {layer:10s} {cnt}")

    import sys

    if summary["untriaged_instances"]:
        print(
            f"WARN: {len(summary['untriaged_instances'])} discovered instance(s) not "
            f"in the gold parquet were NOT triaged: "
            f"{', '.join(summary['untriaged_instances'])}",
            file=sys.stderr,
        )
    bc = summary["budget_check"]
    if not bc["ok"]:
        print(
            f"ERROR: --line-budget {bc['line_budget']} contradicts this run "
            f"(max unique predicted lines: {bc['max_unique_predicted_lines']}; "
            f"instances with impossible line F1 > cap: "
            f"{', '.join(bc['impossible_achieved_over_cap_instances']) or 'none'}). "
            "Artifacts were written, but BUDGET attribution is unreliable — re-run "
            "with the run's actual --line-budget.",
            file=sys.stderr,
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
