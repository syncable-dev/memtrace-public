#!/usr/bin/env python3
"""Reconcile per-task outputs into one manifest-complete prediction JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
from typing import Any

from report import load_manifest_ids, slugify, valid_prediction_row


def failure_stub(instance_id: str, kind: str, message: str) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "traj_data": {
            "pred_steps": [],
            "pred_files": [],
            "pred_spans": {},
            "pred_symbols": {},
        },
        "model_patch": "",
        "harness_failure": {
            "kind": kind,
            "message": message,
            "run_fingerprint": "evaluation_merge",
        },
    }


def load_prediction(path: Path, instance_id: str) -> tuple[dict[str, Any], dict[str, str] | None]:
    if not path.is_file() or path.stat().st_size == 0:
        message = "per-instance prediction is missing or empty"
        return failure_stub(instance_id, "missing_prediction", message), {
            "instance_id": instance_id,
            "kind": "missing_prediction",
            "message": message,
        }
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as error:
        message = f"per-instance prediction is invalid JSONL: {error}"
        return failure_stub(instance_id, "invalid_prediction", message), {
            "instance_id": instance_id,
            "kind": "invalid_prediction",
            "message": message,
        }
    if len(rows) != 1 or not isinstance(rows[0], dict):
        message = f"per-instance prediction must contain exactly one object; found {len(rows)}"
        return failure_stub(instance_id, "invalid_prediction", message), {
            "instance_id": instance_id,
            "kind": "invalid_prediction",
            "message": message,
        }
    row = rows[0]
    if str(row.get("instance_id")) != instance_id or not valid_prediction_row(row):
        message = "per-instance prediction has the wrong instance_id or malformed trajectory"
        return failure_stub(instance_id, "invalid_prediction", message), {
            "instance_id": instance_id,
            "kind": "invalid_prediction",
            "message": message,
        }
    failure = row.get("harness_failure")
    if isinstance(failure, dict):
        return row, {
            "instance_id": instance_id,
            "kind": str(failure.get("kind") or "unspecified"),
            "message": str(failure.get("message") or "runner recorded a harness failure"),
        }
    return row, None


def atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, separators=(",", ":")) + "\n")
    temporary.replace(path)


def reconcile(results: Path) -> dict[str, int]:
    manifest_ids = load_manifest_ids(results / "manifest.json")
    audit_dir = results / "predictions-audit"
    if audit_dir.exists():
        shutil.rmtree(audit_dir)
    audit_dir.mkdir()

    predictions: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for instance_id in manifest_ids:
        slug = slugify(instance_id)
        run_dir = results / "runs" / slug
        prediction, failure = load_prediction(run_dir / "prediction.jsonl", instance_id)
        predictions.append(prediction)
        if failure is not None:
            failures.append(failure)

        source_audit = run_dir / "prediction-audit" / f"{slug}.json"
        destination_audit = audit_dir / f"{slug}.json"
        if source_audit.is_file():
            shutil.copy(source_audit, destination_audit)
        else:
            destination_audit.write_text(
                json.dumps(
                    {
                        "harness_failure": failure
                        or {
                            "instance_id": instance_id,
                            "kind": "missing_audit",
                            "message": "prediction had no audit artifact",
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

    atomic_write_jsonl(results / "predictions.jsonl", predictions)
    atomic_write_jsonl(results / "evaluation-failures.jsonl", failures)
    return {
        "manifest_instances": len(manifest_ids),
        "prediction_records": len(predictions),
        "failure_records": len(failures),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", required=True, type=Path)
    args = parser.parse_args()
    summary = reconcile(args.results_dir)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
