#!/usr/bin/env python3
"""Convert an official SWE-bench run report to ContextBench Pass@1 JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def result_rows(
    frame: pd.DataFrame,
    report: dict,
    submitted: set[str],
    include_all_dataset: bool = False,
) -> list[dict]:
    resolved = set(report.get("resolved_ids", []))
    completed = set(report.get("completed_ids", []))
    unresolved = set(report.get("unresolved_ids", []))
    empty = set(report.get("empty_patch_ids", []))
    errors = set(report.get("error_ids", []))
    if not include_all_dataset:
        frame = frame[frame["original_inst_id"].isin(submitted)]
    results: list[dict] = []
    for row in frame.to_dict(orient="records"):
        original_id = str(row["original_inst_id"])
        if original_id in resolved:
            status = "resolved"
        elif original_id in unresolved:
            status = "unresolved"
        elif original_id in empty:
            status = "empty_patch"
        elif original_id in errors:
            status = "error"
        elif original_id in completed:
            status = "completed_unclassified"
        elif original_id not in submitted:
            status = "not_submitted"
        else:
            status = "missing"
        results.append(
            {
                "instance_id": row["instance_id"],
                "original_inst_id": original_id,
                "resolved": original_id in resolved,
                "status": status,
            }
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--swebench-predictions", type=Path, required=True)
    parser.add_argument(
        "--include-all-dataset",
        action="store_true",
        help="Count dataset rows without submitted patches as unresolved.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    frame = pd.read_parquet(args.dataset, columns=["instance_id", "original_inst_id"])
    submitted = {
        str(json.loads(line)["instance_id"])
        for line in args.swebench_predictions.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as output:
        for row in result_rows(
            frame, report, submitted, include_all_dataset=args.include_all_dataset
        ):
            output.write(
                json.dumps(row, separators=(",", ":"))
                + "\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
