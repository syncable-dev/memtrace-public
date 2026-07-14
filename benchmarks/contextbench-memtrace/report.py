#!/usr/bin/env python3
"""Create a paper-compatible ContextBench leaderboard report."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
from statistics import mean
from typing import Any, Iterable


DEFAULT_INPUT_PRICE_PER_MILLION = 1.25
DEFAULT_CACHED_INPUT_PRICE_PER_MILLION = 0.125
DEFAULT_OUTPUT_PRICE_PER_MILLION = 10.0
PRICING_SOURCE = "https://platform.openai.com/docs/pricing"
PRICING_CHECKED_AT = "2026-07-10"
GRANULARITIES = {"file": "file", "block": "symbol", "line": "line"}


class CompletenessError(ValueError):
    """The manifest universe cannot be reconciled with the run artifacts."""


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_manifest_ids(path: Path) -> list[str]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    items = loaded.get("tasks") if isinstance(loaded, dict) else loaded
    if not isinstance(items, list):
        raise CompletenessError("manifest must be a JSON list or an object with tasks[]")
    ids: list[str] = []
    for index, item in enumerate(items):
        value = item.get("instance_id") if isinstance(item, dict) else item
        if value is None or not str(value).strip():
            raise CompletenessError(f"manifest item {index} has no instance_id")
        ids.append(str(value))
    if not ids:
        raise CompletenessError("manifest contains no tasks")
    duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise CompletenessError(f"manifest contains duplicate instance_id values: {duplicates}")
    return ids


def index_rows(rows: Iterable[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CompletenessError(f"{label} row {index} is not a JSON object")
        value = row.get("instance_id") or row.get("original_inst_id")
        if value is None or not str(value).strip():
            raise CompletenessError(f"{label} row {index} has no instance_id")
        instance_id = str(value)
        if instance_id in indexed:
            duplicates.add(instance_id)
        indexed[instance_id] = row
    if duplicates:
        raise CompletenessError(f"{label} contains duplicate instance_id values: {sorted(duplicates)}")
    return indexed


def reconcile_rows(
    manifest_ids: list[str],
    rows: Iterable[dict[str, Any]],
    label: str,
    allow_partial: bool,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    indexed = index_rows(rows, label)
    expected = set(manifest_ids)
    extras = sorted(set(indexed) - expected)
    if extras:
        raise CompletenessError(f"{label} contains instance IDs outside the manifest: {extras}")
    missing = [instance_id for instance_id in manifest_ids if instance_id not in indexed]
    if missing and not allow_partial:
        raise CompletenessError(
            f"{label} is missing {len(missing)}/{len(manifest_ids)} manifest instances: {missing}"
        )
    return indexed, missing


def valid_metric_result(result: dict[str, Any]) -> bool:
    if result.get("error"):
        return True
    for result_key in GRANULARITIES.values():
        metric = result.get("final", {}).get(result_key)
        if not isinstance(metric, dict):
            return False
        for key in ("coverage", "precision"):
            value = metric.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False
            if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                return False
    efficiency = result.get("trajectory", {}).get("auc_coverage", {}).get("line")
    if not isinstance(efficiency, (int, float)) or isinstance(efficiency, bool):
        return False
    return math.isfinite(float(efficiency)) and 0.0 <= float(efficiency) <= 1.0


def valid_prediction_row(prediction: dict[str, Any]) -> bool:
    trajectory = prediction.get("traj_data")
    if not isinstance(trajectory, dict):
        return False
    steps = trajectory.get("pred_steps")
    if not isinstance(steps, list):
        return False
    if any(
        not isinstance(step, dict) or not isinstance(step.get("spans", {}), dict)
        for step in steps
    ):
        return False
    failure = prediction.get("harness_failure")
    return failure is None or isinstance(failure, dict)


def zero_prediction(instance_id: str, reason: str) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "traj_data": {"pred_steps": []},
        "harness_failure": {"kind": reason},
    }


def zero_result(instance_id: str, reason: str) -> dict[str, Any]:
    return {"instance_id": instance_id, "error": reason}


def harmonic_mean(recall: float, precision: float) -> float:
    if recall + precision == 0:
        return 0.0
    return 2 * recall * precision / (recall + precision)


def macro_context_metrics(results: Iterable[dict[str, Any]]) -> dict[str, dict[str, float]]:
    rows = list(results)
    summary: dict[str, dict[str, float]] = {}
    for label, result_key in GRANULARITIES.items():
        recalls: list[float] = []
        precisions: list[float] = []
        f1_scores: list[float] = []
        for result in rows:
            if result.get("error"):
                recalls.append(0.0)
                precisions.append(0.0)
                f1_scores.append(0.0)
                continue
            metric = result.get("final", {}).get(result_key)
            if not metric:
                continue
            recall = float(metric["coverage"])
            precision = float(metric["precision"])
            recalls.append(recall)
            precisions.append(precision)
            f1_scores.append(harmonic_mean(recall, precision))
        if recalls:
            summary[label] = {
                "recall": mean(recalls),
                "precision": mean(precisions),
                "f1": mean(f1_scores),
            }
    return summary


def merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def step_lines(step: dict[str, Any]) -> dict[str, list[tuple[int, int]]]:
    result: dict[str, list[tuple[int, int]]] = {}
    for file_path, spans in step.get("spans", {}).items():
        intervals = [
            (int(span.get("start", span.get("start_line", 1))), int(span.get("end", span.get("end_line", 1))))
            for span in spans
        ]
        result[file_path] = merge_intervals(intervals)
    return result


def interval_size(intervals: Iterable[tuple[int, int]]) -> int:
    return sum(end - start + 1 for start, end in merge_intervals(intervals))


def line_map_size(lines: dict[str, list[tuple[int, int]]]) -> int:
    return sum(interval_size(intervals) for intervals in lines.values())


def intersection_size(
    left: dict[str, list[tuple[int, int]]], right: dict[str, list[tuple[int, int]]]
) -> int:
    total = 0
    for file_path in left.keys() & right.keys():
        a = merge_intervals(left[file_path])
        b = merge_intervals(right[file_path])
        i = j = 0
        while i < len(a) and j < len(b):
            start = max(a[i][0], b[j][0])
            end = min(a[i][1], b[j][1])
            if start <= end:
                total += end - start + 1
            if a[i][1] < b[j][1]:
                i += 1
            else:
                j += 1
    return total


def union_line_maps(
    left: dict[str, list[tuple[int, int]]], right: dict[str, list[tuple[int, int]]]
) -> dict[str, list[tuple[int, int]]]:
    return {
        file_path: merge_intervals(left.get(file_path, []) + right.get(file_path, []))
        for file_path in left.keys() | right.keys()
    }


def trajectory_patterns(predictions: Iterable[dict[str, Any]]) -> dict[str, float]:
    steps_per_instance: list[int] = []
    lines_per_step: list[float] = []
    redundancies: list[float] = []
    for prediction in predictions:
        steps = prediction.get("traj_data", {}).get("pred_steps", [])
        steps_per_instance.append(len(steps))
        line_maps = [step_lines(step) for step in steps]
        counts = [line_map_size(lines) for lines in line_maps]
        lines_per_step.append(mean(counts) if counts else 0.0)

        seen: dict[str, list[tuple[int, int]]] = {}
        per_step_redundancy: list[float] = []
        for index, lines in enumerate(line_maps):
            size = line_map_size(lines)
            if index > 0:
                per_step_redundancy.append(intersection_size(lines, seen) / size if size else 0.0)
            seen = union_line_maps(seen, lines)
        redundancies.append(mean(per_step_redundancy) if per_step_redundancy else 0.0)
    return {
        "steps": mean(steps_per_instance) if steps_per_instance else 0.0,
        "lines": mean(lines_per_step) if lines_per_step else 0.0,
        "redundancy": mean(redundancies) if redundancies else 0.0,
    }


def macro_efficiency(results: Iterable[dict[str, Any]]) -> float | None:
    values: list[float] = []
    for result in results:
        if result.get("error"):
            values.append(0.0)
            continue
        value = result.get("trajectory", {}).get("auc_coverage", {}).get("line")
        if value is not None:
            values.append(float(value))
    return mean(values) if values else None


def usage_cost(
    usage: dict[str, Any], input_price: float, cached_input_price: float, output_price: float
) -> float:
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    cached_tokens = int(usage.get("cached_input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    uncached_tokens = max(0, input_tokens - cached_tokens)
    return (
        uncached_tokens * input_price
        + cached_tokens * cached_input_price
        + output_tokens * output_price
    ) / 1_000_000


def audit_costs(
    audit_dir: Path,
    input_price: float,
    cached_input_price: float,
    output_price: float,
    expected_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    costs: list[float] = []
    incomplete: list[str] = []
    paths = sorted(audit_dir.glob("*.json"))
    expected_paths: dict[str, str] = {}
    missing: list[str] = []
    unexpected: list[str] = []
    if expected_ids is not None:
        expected_ids_list = list(expected_ids)
        expected_paths = {slugify(instance_id): instance_id for instance_id in expected_ids_list}
        if len(expected_paths) != len(expected_ids_list):
            raise CompletenessError("manifest instance IDs collide after audit filename normalization")
        actual_stems = {path.stem for path in paths}
        missing = sorted(expected_paths[stem] for stem in set(expected_paths) - actual_stems)
        unexpected = sorted(actual_stems - set(expected_paths))
    for path in paths:
        audit = json.loads(path.read_text(encoding="utf-8"))
        task_cost = 0.0
        planner = audit.get("search_planner") or {}
        selector = audit.get("selector") or {}
        instance_id = expected_paths.get(path.stem, path.stem)
        if audit.get("harness_failure"):
            incomplete.append(instance_id)
        if planner.get("source") == "locked_query_plan":
            incomplete.append(instance_id)
        elif planner:
            task_cost += usage_cost(planner, input_price, cached_input_price, output_price)
        if selector:
            task_cost += usage_cost(selector, input_price, cached_input_price, output_price)
        agent = audit.get("agent") or {}
        task_cost += float(agent.get("cost_usd", 0.0) or 0.0)
        costs.append(task_cost)
    return {
        "average_usd": (
            mean(costs) if costs and not incomplete and not missing and not unexpected else None
        ),
        "observed_average_usd": mean(costs) if costs else None,
        "complete": bool(costs) and not incomplete and not missing and not unexpected,
        "incomplete_instances": sorted(set(incomplete)),
        "missing_instances": missing,
        "unexpected_files": unexpected,
        "audit_count": len(costs),
    }


def slugify(instance_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", instance_id)


def load_pass_at_1(
    path: Path | None,
    manifest_ids: list[str],
    allow_partial: bool,
) -> tuple[float | None, int, list[str]]:
    if path is None:
        return None, 0, []
    rows = load_jsonl(path)
    indexed, missing = reconcile_rows(manifest_ids, rows, "pass results", allow_partial)
    values: list[float] = []
    for instance_id in manifest_ids:
        row = indexed.get(instance_id)
        if row is None:
            values.append(0.0)
            continue
        value = row.get("resolved", row.get("passed", row.get("pass_at_1")))
        if value is None:
            raise CompletenessError(f"pass result for {instance_id} has no outcome")
        if isinstance(value, bool):
            values.append(1.0 if value else 0.0)
        elif isinstance(value, (int, float)) and value in (0, 1):
            values.append(float(value))
        else:
            raise CompletenessError(
                f"pass result for {instance_id} must be boolean or 0/1, got {value!r}"
            )
    return (mean(values), len(indexed), missing)


def create_report(
    predictions: list[dict[str, Any]],
    results: list[dict[str, Any]],
    manifest_ids: list[str],
    audit_dir: Path,
    model: str,
    pass_results: Path | None,
    input_price: float,
    cached_input_price: float,
    output_price: float,
    allow_partial: bool = False,
) -> dict[str, Any]:
    prediction_index, missing_predictions = reconcile_rows(
        manifest_ids, predictions, "predictions", allow_partial
    )
    result_index, missing_results = reconcile_rows(
        manifest_ids, results, "evaluation results", allow_partial
    )

    invalid_predictions = sorted(
        instance_id
        for instance_id, prediction in prediction_index.items()
        if not valid_prediction_row(prediction)
    )
    invalid_results = sorted(
        instance_id
        for instance_id, result in result_index.items()
        if not valid_metric_result(result)
    )
    if invalid_predictions and not allow_partial:
        raise CompletenessError(
            f"predictions contain malformed records for: {invalid_predictions}"
        )
    if invalid_results and not allow_partial:
        raise CompletenessError(
            f"evaluation results contain malformed metric records for: {invalid_results}"
        )

    ordered_predictions: list[dict[str, Any]] = []
    effective_results: list[dict[str, Any]] = []
    zero_reasons: dict[str, list[str]] = {}
    harness_failure_kinds: Counter[str] = Counter()
    reconciled_failures: list[str] = []
    evaluator_errors: dict[str, str] = {}
    invalid_prediction_set = set(invalid_predictions)
    invalid_result_set = set(invalid_results)

    for instance_id in manifest_ids:
        prediction = prediction_index.get(instance_id)
        result = result_index.get(instance_id)
        reasons: list[str] = []

        if prediction is None:
            prediction = zero_prediction(instance_id, "missing_prediction")
            reasons.append("missing_prediction")
        elif instance_id in invalid_prediction_set:
            prediction = zero_prediction(instance_id, "invalid_prediction")
            reasons.append("invalid_prediction")
        failure = prediction.get("harness_failure")
        if isinstance(failure, dict):
            kind = str(failure.get("kind") or "unspecified")
            harness_failure_kinds[kind] += 1
            if failure.get("run_fingerprint") == "evaluation_merge":
                reconciled_failures.append(instance_id)
            reasons.append(f"harness_failure:{kind}")
        ordered_predictions.append(prediction)

        if result is None:
            reasons.append("missing_evaluation_result")
        elif instance_id in invalid_result_set:
            reasons.append("invalid_evaluation_result")
        elif result.get("error"):
            error = str(result["error"])
            evaluator_errors[instance_id] = error
            reasons.append(f"evaluator_error:{error}")

        if reasons:
            zero_reasons[instance_id] = list(dict.fromkeys(reasons))
            effective_results.append(zero_result(instance_id, ";".join(zero_reasons[instance_id])))
        else:
            assert result is not None
            effective_results.append(result)

    if reconciled_failures and not allow_partial:
        raise CompletenessError(
            "evaluation had to synthesize failure records for manifest instances; "
            f"refusing a publishable row: {reconciled_failures}"
        )

    context = macro_context_metrics(effective_results)
    patterns = trajectory_patterns(ordered_predictions)
    costs = audit_costs(
        audit_dir,
        input_price,
        cached_input_price,
        output_price,
        expected_ids=manifest_ids,
    )
    pass_at_1, pass_count, missing_pass_results = load_pass_at_1(
        pass_results, manifest_ids, allow_partial
    )
    artifact_complete = not any(
        (missing_predictions, missing_results, invalid_predictions, invalid_results)
    )
    source_artifact_complete = artifact_complete and not reconciled_failures
    pass_complete = pass_results is None or not missing_pass_results
    valid_count = len(manifest_ids) - len(zero_reasons)
    return {
        "leaderboard": {
            "model": model,
            **context,
            "pass_at_1": pass_at_1,
            "steps": patterns["steps"],
            "lines": patterns["lines"],
            "efficiency": macro_efficiency(effective_results),
            "redundancy": patterns["redundancy"],
            "cost_usd": costs["average_usd"],
        },
        "completeness": {
            "manifest_instances": len(manifest_ids),
            "predictions": len(prediction_index),
            "missing_predictions": missing_predictions,
            "invalid_predictions": invalid_predictions,
            "harness_failure_predictions": sum(harness_failure_kinds.values()),
            "harness_failure_kinds": dict(sorted(harness_failure_kinds.items())),
            "evaluation_reconciled_failure_predictions": reconciled_failures,
            "evaluation_results": len(result_index),
            "missing_evaluation_results": missing_results,
            "invalid_evaluation_results": invalid_results,
            "evaluator_error_results": len(evaluator_errors),
            "evaluator_errors": evaluator_errors,
            "valid_evaluation_results": valid_count,
            "zero_accounted_instances": len(zero_reasons),
            "zero_accounted_reasons": zero_reasons,
            "pass_results": pass_count,
            "missing_pass_results": missing_pass_results,
            "audit_results": costs["audit_count"],
            "cost_complete": costs["complete"],
            "cost_incomplete_instances": costs["incomplete_instances"],
            "cost_missing_instances": costs["missing_instances"],
            "cost_unexpected_files": costs["unexpected_files"],
            "observed_average_cost_usd": costs["observed_average_usd"],
            "artifact_complete": artifact_complete,
            "source_artifact_complete": source_artifact_complete,
            "score_publishable": source_artifact_complete and pass_complete and not allow_partial,
            "allow_partial": allow_partial,
        },
        "metric_contract": {
            "aggregation": "macro average over the manifest task universe",
            "failure_accounting": (
                "harness failures, evaluator errors, and explicitly allowed missing artifacts "
                "contribute zero recall, precision, F1, and efficiency"
            ),
            "block_definition": "definition-level AST symbols",
            "efficiency": "macro line-level AUC-Coverage over observation steps",
            "redundancy": "macro mean of per-step line overlap with prior observations",
            "pass_at_1": "test-passing patch rate; null for retrieval-only runs",
        },
        "pricing": {
            "model": model,
            "input_per_million_usd": input_price,
            "cached_input_per_million_usd": cached_input_price,
            "output_per_million_usd": output_price,
            "source": PRICING_SOURCE,
            "checked_at": PRICING_CHECKED_AT,
        },
    }


def format_value(value: float | None, digits: int = 3) -> str:
    if value is None or not math.isfinite(value):
        return "N/A"
    return f"{value:.{digits}f}"


def print_table(report: dict[str, Any]) -> None:
    row = report["leaderboard"]
    values = [row["model"]]
    for granularity in ("file", "block", "line"):
        values.extend(format_value(row[granularity][key]) for key in ("recall", "precision", "f1"))
    values.extend(
        [
            "N/A" if row["pass_at_1"] is None else f"{100 * row['pass_at_1']:.1f}%",
            format_value(row["steps"]),
            format_value(row["lines"]),
            format_value(row["efficiency"]),
            format_value(row["redundancy"]),
            "N/A" if row["cost_usd"] is None else f"${row['cost_usd']:.2f}",
        ]
    )
    print("\t".join(values))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="authoritative JSON task universe; the report refuses denominator gaps",
    )
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--pass-results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "diagnostic only: zero-fill missing/malformed manifest records and mark the "
            "report non-publishable instead of refusing output"
        ),
    )
    parser.add_argument("--input-price", type=float, default=DEFAULT_INPUT_PRICE_PER_MILLION)
    parser.add_argument(
        "--cached-input-price", type=float, default=DEFAULT_CACHED_INPUT_PRICE_PER_MILLION
    )
    parser.add_argument("--output-price", type=float, default=DEFAULT_OUTPUT_PRICE_PER_MILLION)
    args = parser.parse_args()

    args.output.unlink(missing_ok=True)
    try:
        report = create_report(
            load_jsonl(args.predictions),
            load_jsonl(args.results),
            load_manifest_ids(args.manifest),
            args.audit_dir,
            args.model,
            args.pass_results,
            args.input_price,
            args.cached_input_price,
            args.output_price,
            allow_partial=args.allow_partial,
        )
    except (CompletenessError, json.JSONDecodeError, OSError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
