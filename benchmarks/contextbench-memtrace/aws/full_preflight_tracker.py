#!/usr/bin/env python3
"""Create and update the full ContextBench repository-preflight tracker.

The tracker is a rendered view. The JSONL state is authoritative. Concurrent
collectors serialize complete state transactions. Non-terminal observations are
applied in source-time order, while a verified success is monotonic across
duplicate repair races.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pandas as pd


STAGES = (
    "dataset",
    "checkout",
    "index_embeddings",
    "cache_sealed",
    "mcp",
    "codex",
    "evaluator",
)
PREFLIGHT_STAGES = STAGES[:5]
STATUSES = {"PENDING", "RUNNING", "PASS", "FAIL"}
REQUIRED_COLUMNS = {
    "instance_id",
    "repo",
    "repo_url",
    "language",
    "base_commit",
    "source",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_dataset(path: Path, expected_total: int) -> pd.DataFrame:
    frame = pd.read_parquet(path)
    missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError(f"dataset is missing required columns: {missing}")
    if len(frame) != expected_total:
        raise ValueError(f"expected {expected_total} tasks, found {len(frame)}")
    if frame["instance_id"].duplicated().any():
        duplicates = frame.loc[frame["instance_id"].duplicated(), "instance_id"].tolist()
        raise ValueError(f"duplicate instance ids: {duplicates[:5]}")
    for column in REQUIRED_COLUMNS:
        if frame[column].isna().any() or (frame[column].astype(str).str.len() == 0).any():
            raise ValueError(f"dataset has missing values in required column {column}")
    return frame


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        instance_id = str(record.get("instance_id") or "")
        if not instance_id:
            raise ValueError(f"state line {line_number} has no instance_id")
        if instance_id in records:
            raise ValueError(f"duplicate state record for {instance_id}")
        records[instance_id] = record
    return records


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        Path(temporary_name).replace(path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


@contextmanager
def state_lock(path: Path) -> Iterator[None]:
    """Serialize complete state read/modify/write transactions."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_state(path: Path, records: dict[str, dict[str, Any]]) -> None:
    lines = [json.dumps(records[key], sort_keys=True) for key in sorted(records)]
    atomic_write(path, "\n".join(lines) + "\n")


def preflight_observed_at_unix_ns(preflight: dict[str, Any]) -> int:
    value = preflight.get("completed_at_unix_ns") or preflight.get("updated_at_unix_ns") or 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def new_record(row: Any) -> dict[str, Any]:
    now = utc_now()
    return {
        "instance_id": str(row.instance_id),
        "repo": str(row.repo),
        "repo_url": str(row.repo_url),
        "language": str(row.language),
        "base_commit": str(row.base_commit),
        "source": str(row.source),
        "stages": {
            "dataset": {"status": "PASS", "detail": "required metadata present", "updated_at": now},
            **{
                stage: {"status": "PENDING", "detail": "", "updated_at": now}
                for stage in STAGES
                if stage != "dataset"
            },
        },
        "host": "",
        "run_id": "",
        "updated_at": now,
    }


def reconcile(frame: pd.DataFrame, existing: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for row in frame.itertuples(index=False):
        instance_id = str(row.instance_id)
        record = existing.get(instance_id, new_record(row))
        record.update(
            {
                "instance_id": instance_id,
                "repo": str(row.repo),
                "repo_url": str(row.repo_url),
                "language": str(row.language),
                "base_commit": str(row.base_commit),
                "source": str(row.source),
            }
        )
        record.setdefault("stages", {})
        for stage in STAGES:
            default_status = "PASS" if stage == "dataset" else "PENDING"
            record["stages"].setdefault(
                stage,
                {"status": default_status, "detail": "", "updated_at": utc_now()},
            )
        records[instance_id] = record
    unknown = sorted(set(existing) - set(records))
    if unknown:
        raise ValueError(f"state contains tasks absent from dataset: {unknown[:5]}")
    return records


def overall_status(record: dict[str, Any]) -> str:
    statuses = [record["stages"][stage]["status"] for stage in STAGES]
    if "FAIL" in statuses:
        return "FAIL"
    if all(status == "PASS" for status in statuses):
        return "PASS"
    if "RUNNING" in statuses:
        return "RUNNING"
    return "PENDING"


def preflight_status(record: dict[str, Any]) -> str:
    statuses = [record["stages"][stage]["status"] for stage in PREFLIGHT_STAGES]
    if "FAIL" in statuses:
        return "FAIL"
    if all(status == "PASS" for status in statuses):
        return "PASS"
    if "RUNNING" in statuses:
        return "RUNNING"
    return "PENDING"


def markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def render(records: dict[str, dict[str, Any]], dataset: Path, state: Path) -> str:
    ordered = sorted(records.values(), key=lambda record: record["instance_id"])
    overall = Counter(overall_status(record) for record in ordered)
    preflight = Counter(preflight_status(record) for record in ordered)
    stage_counts = {
        stage: Counter(record["stages"][stage]["status"] for record in ordered)
        for stage in STAGES
    }
    repo_count = len({record["repo_url"].rstrip("/") for record in ordered})
    snapshot_count = len(
        {(record["repo_url"].rstrip("/"), record["base_commit"]) for record in ordered}
    )
    lines = [
        "# ContextBench full-run preflight tracker",
        "",
        f"Generated: `{utc_now()}`",
        f"Dataset: `{dataset}`",
        f"State: `{state}`",
        "",
        "## Gate",
        "",
        "The 60-agent, 1,136-task benchmark may start only when every row's preflight is `PASS`. "
        "Preflight requires the exact checkout, Memtrace index and embeddings, sealed reusable cache, "
        "and a task-scoped MCP call. Final completion additionally requires Codex and the official evaluator.",
        "",
        f"- Tasks: **{len(ordered)}**",
        f"- Unique repositories: **{repo_count}**",
        f"- Unique repository snapshots: **{snapshot_count}**",
        f"- Preflight: **{preflight['PASS']} pass / {preflight['RUNNING']} running / "
        f"{preflight['FAIL']} fail / {preflight['PENDING']} pending**",
        f"- Final: **{overall['PASS']} pass / {overall['RUNNING']} running / "
        f"{overall['FAIL']} fail / {overall['PENDING']} pending**",
        "",
        "| Stage | Pass | Running | Fail | Pending |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for stage in STAGES:
        counts = stage_counts[stage]
        lines.append(
            f"| {stage} | {counts['PASS']} | {counts['RUNNING']} | {counts['FAIL']} | {counts['PENDING']} |"
        )
    lines.extend(
        [
            "",
            "## Tasks",
            "",
            "| # | Task | Source | Language | Repository | Commit | Checkout | Index+embed | Cache | MCP | Preflight | Codex | Eval | Final | Host/run | Detail |",
            "| ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for index, record in enumerate(ordered, 1):
        stages = record["stages"]
        details = [
            f"{stage}: {stages[stage].get('detail', '')}"
            for stage in STAGES
            if stages[stage].get("detail")
            and not (stage == "dataset" and stages[stage].get("detail") == "required metadata present")
        ]
        host_run = "/".join(part for part in (record.get("host"), record.get("run_id")) if part)
        lines.append(
            "| "
            + " | ".join(
                markdown_cell(value)
                for value in (
                    index,
                    record["instance_id"],
                    record["source"],
                    record["language"],
                    record["repo"],
                    record["base_commit"][:12],
                    stages["checkout"]["status"],
                    stages["index_embeddings"]["status"],
                    stages["cache_sealed"]["status"],
                    stages["mcp"]["status"],
                    preflight_status(record),
                    stages["codex"]["status"],
                    stages["evaluator"]["status"],
                    overall_status(record),
                    host_run,
                    "; ".join(details),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def command_init(args: argparse.Namespace) -> None:
    frame = read_dataset(args.dataset, args.expected_total)
    records = reconcile(frame, load_state(args.state))
    write_state(args.state, records)
    atomic_write(args.tracker, render(records, args.dataset, args.state))
    print(f"initialized {len(records)} tasks in {args.state}")
    print(f"rendered {args.tracker}")


def command_set(args: argparse.Namespace) -> None:
    if args.status not in STATUSES:
        raise ValueError(f"invalid status {args.status}; expected one of {sorted(STATUSES)}")
    if args.stage not in STAGES:
        raise ValueError(f"invalid stage {args.stage}; expected one of {STAGES}")
    frame = read_dataset(args.dataset, args.expected_total)
    records = reconcile(frame, load_state(args.state))
    if args.instance_id not in records:
        raise ValueError(f"unknown instance id {args.instance_id}")
    record = records[args.instance_id]
    record["stages"][args.stage] = {
        "status": args.status,
        "detail": args.detail,
        "updated_at": utc_now(),
    }
    if args.host is not None:
        record["host"] = args.host
    if args.run_id is not None:
        record["run_id"] = args.run_id
    record["updated_at"] = utc_now()
    write_state(args.state, records)
    atomic_write(args.tracker, render(records, args.dataset, args.state))
    print(f"updated {args.instance_id} {args.stage}={args.status}")


def command_import(args: argparse.Namespace) -> None:
    frame = read_dataset(args.dataset, args.expected_total)
    records = reconcile(frame, load_state(args.state))
    imported = 0
    skipped_stale = 0
    skipped_non_improving = 0
    for record_path in sorted(args.records.glob("*.json")):
        preflight = json.loads(record_path.read_text())
        instance_id = str(preflight.get("instance_id") or "")
        if instance_id not in records:
            raise ValueError(f"unknown preflight instance id in {record_path}: {instance_id}")
        target = records[instance_id]
        observed_at = preflight_observed_at_unix_ns(preflight)
        current_observed_at = int(target.get("preflight_observed_at_unix_ns") or 0)
        terminal_status = str(preflight.get("status") or "")
        current_success = preflight_status(target) == "PASS"
        incoming_success = terminal_status == "success"
        if current_success and not incoming_success:
            skipped_non_improving += 1
            continue
        if (
            observed_at
            and current_observed_at
            and observed_at < current_observed_at
            and not (incoming_success and not current_success)
        ):
            skipped_stale += 1
            continue
        for stage_name, stage_value in (preflight.get("stages") or {}).items():
            if stage_name not in PREFLIGHT_STAGES or not isinstance(stage_value, dict):
                continue
            status = str(stage_value.get("status") or "")
            if status not in STATUSES:
                raise ValueError(f"invalid {stage_name} status in {record_path}: {status}")
            target["stages"][stage_name] = {
                "status": status,
                "detail": str(stage_value.get("detail") or ""),
                "updated_at": utc_now(),
            }
        if terminal_status == "success":
            incomplete = [
                stage_name
                for stage_name in PREFLIGHT_STAGES
                if target["stages"][stage_name]["status"] != "PASS"
            ]
            if incomplete:
                raise ValueError(
                    f"terminal success record in {record_path} has incomplete stages: {incomplete}"
                )
        elif terminal_status == "failure":
            if not any(
                target["stages"][stage_name]["status"] == "FAIL"
                for stage_name in PREFLIGHT_STAGES
            ):
                raise ValueError(
                    f"terminal failure record in {record_path} has no failed stage"
                )
            for stage_name in PREFLIGHT_STAGES:
                if target["stages"][stage_name]["status"] == "RUNNING":
                    target["stages"][stage_name] = {
                        "status": "PENDING",
                        "detail": "terminal failure record did not complete this stage",
                        "updated_at": utc_now(),
                    }
        target["host"] = str(preflight.get("host") or target.get("host") or "")
        target["run_id"] = str(preflight.get("run_id") or target.get("run_id") or "")
        if observed_at:
            target["preflight_observed_at_unix_ns"] = observed_at
        target["updated_at"] = utc_now()
        imported += 1
    write_state(args.state, records)
    atomic_write(args.tracker, render(records, args.dataset, args.state))
    print(
        f"imported {imported} preflight records from {args.records}"
        f"; skipped {skipped_stale} stale observations"
        f"; skipped {skipped_non_improving} non-improving observations"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--tracker", type=Path, required=True)
    parser.add_argument("--expected-total", type=int, default=1136)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init")
    init_parser.set_defaults(func=command_init)
    set_parser = subparsers.add_parser("set")
    set_parser.add_argument("--instance-id", required=True)
    set_parser.add_argument("--stage", required=True, choices=STAGES)
    set_parser.add_argument("--status", required=True, choices=sorted(STATUSES))
    set_parser.add_argument("--detail", default="")
    set_parser.add_argument("--host")
    set_parser.add_argument("--run-id")
    set_parser.set_defaults(func=command_set)
    import_parser = subparsers.add_parser("import-preflight")
    import_parser.add_argument("--records", type=Path, required=True)
    import_parser.set_defaults(func=command_import)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    with state_lock(args.state):
        args.func(args)


if __name__ == "__main__":
    main()
