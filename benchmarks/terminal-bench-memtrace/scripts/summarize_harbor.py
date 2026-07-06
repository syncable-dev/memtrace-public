#!/usr/bin/env python3
import csv
import json
import sys
from pathlib import Path


def iter_json_files(root: Path):
    yield from root.rglob("*.json")


def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def find_task_name(path: Path, payload: dict):
    for key in ("task_name", "task_id", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("task", "dataset_task"):
        value = payload.get(key)
        if isinstance(value, dict):
            for child_key in ("name", "task_name", "id"):
                child = value.get(child_key)
                if isinstance(child, str) and child:
                    return child
    for part in reversed(path.parts):
        if "__" in part:
            return part.split("__", 1)[0]
    return path.parent.name


def find_success(payload: dict):
    for key in ("success", "passed", "resolved", "is_resolved"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    for key in ("reward", "score"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return value > 0
    result = payload.get("result")
    if isinstance(result, dict):
        nested = find_success(result)
        if nested is not None:
            return nested
    verification = payload.get("verification")
    if isinstance(verification, dict):
        nested = find_success(verification)
        if nested is not None:
            return nested
    verifier_result = payload.get("verifier_result")
    if isinstance(verifier_result, dict):
        rewards = verifier_result.get("rewards")
        if isinstance(rewards, dict):
            value = rewards.get("reward")
            if isinstance(value, (int, float)):
                return value > 0
        nested = find_success(verifier_result)
        if nested is not None:
            return nested
    return None


def find_cost(payload: dict):
    for key in ("cost_usd", "total_cost_usd"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    agent = payload.get("agent")
    if isinstance(agent, dict):
        return find_cost(agent)
    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        return find_cost(metrics)
    agent_result = payload.get("agent_result")
    if isinstance(agent_result, dict):
        return find_cost(agent_result)
    return None


def find_exception(payload: dict):
    exception_info = payload.get("exception_info")
    if isinstance(exception_info, dict):
        value = exception_info.get("exception_type")
        if isinstance(value, str) and value:
            return value
    return ""


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: summarize_harbor.py RUN_DIR", file=sys.stderr)
        return 2

    root = Path(sys.argv[1]).resolve()
    if not root.exists():
        print(f"run directory not found: {root}", file=sys.stderr)
        return 2

    rows = []
    seen = set()
    for path in iter_json_files(root):
        payload = load_json(path)
        if not isinstance(payload, dict):
            continue
        success = find_success(payload)
        if success is None:
            continue
        task = find_task_name(path, payload)
        key = (str(path), task)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "task": task,
                "success": int(bool(success)),
                "cost_usd": find_cost(payload),
                "exception": find_exception(payload),
                "path": str(path.relative_to(root)),
            }
        )

    out_path = root / "summary.csv"
    with out_path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["task", "success", "cost_usd", "exception", "path"],
        )
        writer.writeheader()
        writer.writerows(rows)

    successes = sum(row["success"] for row in rows)
    total = len(rows)
    cost = sum(row["cost_usd"] or 0.0 for row in rows)
    rate = successes / total if total else 0.0
    print(f"run_dir={root}")
    print(f"trials={total}")
    print(f"successes={successes}")
    print(f"success_rate={rate:.3f}")
    print(f"observed_cost_usd={cost:.2f}")
    print(f"summary_csv={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
