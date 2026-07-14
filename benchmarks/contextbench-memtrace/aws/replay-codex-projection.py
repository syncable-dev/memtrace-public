#!/usr/bin/env python3
"""Replay task-agnostic Codex context projections from sealed trajectories."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codex_runner  # noqa: E402


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument(
        "--variant", choices=sorted(codex_runner.PROJECTION_POLICIES), required=True
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    rows = read_jsonl(args.checkpoint / "candidate-predictions.jsonl")
    output: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row.get("harness_failure"), dict):
            output.append(row)
            continue
        instance_id = str(row["instance_id"])
        audit = json.loads(
            (args.checkpoint / "prediction-audits" / f"{instance_id}.json").read_text()
        )
        trajectory = row.get("traj_data") or {}
        final = (audit.get("codex") or {}).get("final") or {}
        contexts = codex_runner.project_hierarchical_recall(
            final.get("contexts") or [],
            trajectory.get("pred_steps") or [],
            str(row.get("model_patch") or ""),
            int(audit.get("line_budget") or 200),
            args.variant,
        )
        spans: dict[str, list[dict[str, Any]]] = {}
        for context in contexts:
            spans.setdefault(context["file"], []).append(
                {"type": "line", "start": context["start"], "end": context["end"]}
            )
        output.append(
            {
                **row,
                "traj_data": {
                    **trajectory,
                    "pred_files": list(spans),
                    "pred_spans": spans,
                },
            }
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in output)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
