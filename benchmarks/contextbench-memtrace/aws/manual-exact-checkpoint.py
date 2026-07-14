#!/usr/bin/env python3
"""Freeze and compare an exact completion-order ContextBench checkpoint.

The N=100 comparator deliberately enforces a 100-task treatment contract.
This utility binds the authoritative monitor snapshot receipt for predeclared
agent and repair gates, copies only the exact completion-order predictions, and
compares official evaluator output with identical task IDs from two sealed
controls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics
from typing import Any, Iterable


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def index_rows(path: Path, label: str) -> dict[str, dict[str, Any]]:
    rows = read_jsonl(path)
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        instance_id = str(row.get("instance_id") or "")
        if not instance_id or instance_id in indexed:
            raise ValueError(f"{label} has missing or duplicate instance_id")
        indexed[instance_id] = row
    return indexed


def prepare(args: argparse.Namespace) -> None:
    mirror = args.mirror.resolve(strict=True)
    receipt_path = args.snapshot_receipt.resolve(strict=True)
    previous = args.previous.resolve(strict=True)
    old = args.old_control.resolve(strict=True)
    output = args.output.resolve(strict=False)
    if output.exists():
        raise ValueError(f"refusing to overwrite checkpoint: {output}")

    receipt = read_json(receipt_path)
    terminals = receipt.get("terminals")
    if not isinstance(terminals, list) or len(terminals) < args.count:
        raise ValueError("snapshot receipt has too few terminal tasks")
    selected = terminals[: args.count]
    ids = [str(item.get("instance_id") or "") for item in selected]
    if len(ids) != args.count or len(ids) != len(set(ids)) or not all(ids):
        raise ValueError("snapshot completion order is malformed")

    source_manifest = read_json(mirror / "manifest.json")
    if not isinstance(source_manifest, list) or not set(ids).issubset(
        set(source_manifest)
    ):
        raise ValueError("selected checkpoint IDs are not in the candidate manifest")
    manifest_index = {
        instance_id: index for index, instance_id in enumerate(source_manifest)
    }
    for terminal, instance_id in zip(selected, ids):
        if terminal.get("manifest_index") != manifest_index[instance_id]:
            raise ValueError(f"snapshot manifest index mismatch: {instance_id}")

    receipt_files = {
        str(item.get("path")): item
        for item in receipt.get("files", [])
        if isinstance(item, dict)
    }
    candidate_rows: list[dict[str, Any]] = []
    audits: list[tuple[Path, str]] = []
    for terminal, instance_id in zip(selected, ids):
        slug = str(terminal.get("slug") or "")
        run_dir = mirror / "runs" / slug
        prediction_path = run_dir / "prediction.jsonl"
        audit_path = run_dir / "prediction-audit" / f"{slug}.json"
        relative_prediction = prediction_path.relative_to(mirror).as_posix()
        relative_audit = audit_path.relative_to(mirror).as_posix()
        for path, relative in (
            (prediction_path, relative_prediction),
            (audit_path, relative_audit),
        ):
            receipt_identity = receipt_files.get(relative)
            if not path.is_file() or not isinstance(receipt_identity, dict):
                raise ValueError(f"snapshot omits bound artifact: {relative}")
            if sha256(path) != receipt_identity.get("sha256"):
                raise ValueError(f"snapshot artifact hash mismatch: {relative}")
        rows = read_jsonl(prediction_path)
        if len(rows) != 1 or rows[0].get("instance_id") != instance_id:
            raise ValueError(f"candidate prediction is malformed: {instance_id}")
        audit = read_json(audit_path)
        terminal_status = terminal.get("status")
        is_failure = terminal_status == "failure"
        if is_failure:
            if not isinstance(rows[0].get("harness_failure"), dict) or not isinstance(
                audit.get("harness_failure"), dict
            ):
                raise ValueError(
                    f"candidate failure evidence is malformed: {instance_id}"
                )
        elif terminal_status != "success":
            raise ValueError(f"candidate terminal status is invalid: {instance_id}")
        elif args.agent_policy:
            agent = audit.get("agent")
            localization = (
                agent.get("localization_protocol") if isinstance(agent, dict) else None
            )
            projection = (
                agent.get("final_context_projection")
                if isinstance(agent, dict)
                else None
            )
            legacy_agent_matches = (
                isinstance(localization, dict)
                and localization.get("policy") == args.agent_policy
                and isinstance(projection, dict)
                and projection.get("policy") == args.projection_policy
                and projection.get("line_budget") == args.line_budget
                and projection.get("unique_lines", args.line_budget + 1)
                <= args.line_budget
            )
            codex = audit.get("codex")
            final = codex.get("final") if isinstance(codex, dict) else None
            contexts = final.get("contexts") if isinstance(final, dict) else None
            context_lines: set[tuple[str, int]] = set()
            codex_contexts_valid = isinstance(contexts, list)
            if codex_contexts_valid:
                for context in contexts:
                    if not isinstance(context, dict):
                        codex_contexts_valid = False
                        break
                    file = context.get("file")
                    start = context.get("start")
                    end = context.get("end")
                    if (
                        not isinstance(file, str)
                        or not file
                        or not isinstance(start, int)
                        or isinstance(start, bool)
                        or not isinstance(end, int)
                        or isinstance(end, bool)
                        or start < 1
                        or end < start
                    ):
                        codex_contexts_valid = False
                        break
                    context_lines.update((file, line) for line in range(start, end + 1))
            codex_agent_matches = (
                audit.get("policy") == args.agent_policy
                and audit.get("final_context_policy") == args.projection_policy
                and audit.get("line_budget") == args.line_budget
                and codex_contexts_valid
                and len(context_lines) <= args.line_budget
            )
            if not legacy_agent_matches and not codex_agent_matches:
                raise ValueError(
                    f"candidate agent policy audit mismatch: {instance_id}"
                )
        else:
            post_selector = audit.get("post_selector_policy")
            if (
                not isinstance(post_selector, dict)
                or post_selector.get("policy_name") != args.treatment
                or audit.get("pack_policy") != args.pack_policy
                or audit.get("query_strategy") != args.query_strategy
            ):
                raise ValueError(f"candidate treatment audit mismatch: {instance_id}")
        candidate_rows.append(rows[0])
        audits.append((audit_path, slug))

    controls: dict[
        str, tuple[Path, dict[str, dict[str, Any]], dict[str, dict[str, Any]]]
    ] = {}
    for label, root in (("previous", previous), ("old", old)):
        manifest = read_json(root / "manifest.json")
        if not isinstance(manifest, list) or not set(ids).issubset(set(manifest)):
            raise ValueError(f"{label} control does not contain every checkpoint ID")
        controls[label] = (
            root,
            index_rows(root / "predictions.jsonl", f"{label} predictions"),
            index_rows(root / "results.jsonl", f"{label} results"),
        )

    output.mkdir(parents=True)
    audit_output = output / "prediction-audits"
    audit_output.mkdir()
    write_json(output / "manifest.json", ids)
    write_json(output / "completion-order.json", selected)
    write_jsonl(output / "candidate-predictions.jsonl", candidate_rows)
    for source, slug in audits:
        shutil.copy2(source, audit_output / f"{slug}.json")
    for label, (_, predictions, results) in controls.items():
        write_jsonl(
            output / f"{label}-predictions.jsonl", (predictions[item] for item in ids)
        )
        write_jsonl(output / f"{label}-results.jsonl", (results[item] for item in ids))

    write_json(
        output / "binding.json",
        {
            "schema_version": 1,
            "mode": "exact-completion-order-gate",
            "count": args.count,
            "candidate_run_id": receipt.get("run_id"),
            "snapshot_receipt": str(receipt_path),
            "snapshot_receipt_sha256": sha256(receipt_path),
            "candidate_manifest_sha256": sha256(mirror / "manifest.json"),
            "treatment": (
                {
                    "mode": "agent",
                    "agent_policy": args.agent_policy,
                    "projection_policy": args.projection_policy,
                    "line_budget": args.line_budget,
                }
                if args.agent_policy
                else {
                    "mode": "post-selector",
                    "post_selector_policy": args.treatment,
                    "pack_policy": args.pack_policy,
                    "query_strategy": args.query_strategy,
                }
            ),
            "controls": {
                label: {
                    "root": str(root),
                    "manifest_sha256": sha256(root / "manifest.json"),
                    "predictions_sha256": sha256(root / "predictions.jsonl"),
                    "results_sha256": sha256(root / "results.jsonl"),
                }
                for label, (root, _, _) in controls.items()
            },
        },
    )


def harmonic(recall: float, precision: float) -> float:
    return (
        0.0
        if recall + precision == 0
        else 2 * recall * precision / (recall + precision)
    )


def line_metrics(row: dict[str, Any]) -> dict[str, float]:
    if row.get("error"):
        recall = precision = 0.0
    else:
        line = row.get("final", {}).get("line", {})
        recall = float(line.get("coverage", 0.0))
        precision = float(line.get("precision", 0.0))
    if not all(
        math.isfinite(value) and 0.0 <= value <= 1.0 for value in (recall, precision)
    ):
        raise ValueError("official evaluator emitted invalid line metrics")
    return {"recall": recall, "precision": precision, "f1": harmonic(recall, precision)}


def compare(args: argparse.Namespace) -> None:
    root = args.checkpoint.resolve(strict=True)
    ids = read_json(root / "manifest.json")
    if not isinstance(ids, list) or not ids:
        raise ValueError("checkpoint manifest is malformed")
    indexed = {
        "candidate": index_rows(
            args.candidate_results.resolve(strict=True), "candidate results"
        ),
        "previous": index_rows(root / "previous-results.jsonl", "previous results"),
        "old_control": index_rows(root / "old-results.jsonl", "old results"),
    }
    for label, rows in indexed.items():
        if set(rows) != set(ids):
            raise ValueError(f"{label} result IDs differ from the exact checkpoint")

    paired = []
    by_role: dict[str, list[dict[str, float]]] = {label: [] for label in indexed}
    for instance_id in ids:
        metrics = {
            label: line_metrics(rows[instance_id]) for label, rows in indexed.items()
        }
        for label, value in metrics.items():
            by_role[label].append(value)
        paired.append(
            {
                "instance_id": instance_id,
                **metrics,
                "delta_recall_vs_previous": metrics["candidate"]["recall"]
                - metrics["previous"]["recall"],
                "delta_precision_vs_previous": metrics["candidate"]["precision"]
                - metrics["previous"]["precision"],
                "delta_f1_vs_previous": metrics["candidate"]["f1"]
                - metrics["previous"]["f1"],
            }
        )

    summary = {
        label: {
            field: statistics.fmean(item[field] for item in values)
            for field in ("recall", "precision", "f1")
        }
        for label, values in by_role.items()
    }
    summary["delta_vs_previous"] = {
        field: summary["candidate"][field] - summary["previous"][field]
        for field in ("recall", "precision", "f1")
    }
    summary["delta_vs_old_control"] = {
        field: summary["candidate"][field] - summary["old_control"][field]
        for field in ("recall", "precision", "f1")
    }
    gate_pass = (
        summary["delta_vs_previous"]["recall"] >= -1e-12
        and summary["delta_vs_previous"]["precision"] >= -1e-12
    )
    write_json(
        root / "comparison.json",
        {
            "schema_version": 1,
            "mode": "exact-gate-no-regression",
            "gate_pass": gate_pass,
            "ids_in_completion_order": ids,
            "inputs": {
                "binding_sha256": sha256(root / "binding.json"),
                "candidate_results_sha256": sha256(args.candidate_results),
                "previous_results_sha256": sha256(root / "previous-results.jsonl"),
                "old_results_sha256": sha256(root / "old-results.jsonl"),
            },
            "summary": summary,
            "paired": paired,
        },
    )
    print(json.dumps({"gate_pass": gate_pass, "summary": summary}, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("prepare")
    freeze.add_argument("--mirror", required=True, type=Path)
    freeze.add_argument("--snapshot-receipt", required=True, type=Path)
    freeze.add_argument("--previous", required=True, type=Path)
    freeze.add_argument("--old-control", required=True, type=Path)
    freeze.add_argument("--output", required=True, type=Path)
    freeze.add_argument("--count", required=True, type=int)
    mode = freeze.add_mutually_exclusive_group(required=True)
    mode.add_argument("--treatment")
    mode.add_argument("--agent-policy")
    freeze.add_argument("--pack-policy")
    freeze.add_argument("--query-strategy")
    freeze.add_argument("--projection-policy")
    freeze.add_argument("--line-budget", type=int)
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--checkpoint", required=True, type=Path)
    compare_parser.add_argument("--candidate-results", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "prepare":
        if args.agent_policy:
            if (
                not args.projection_policy
                or not args.line_budget
                or args.line_budget < 1
            ):
                raise SystemExit(
                    "agent prepare requires --projection-policy and positive --line-budget"
                )
        elif not args.pack_policy or not args.query_strategy:
            raise SystemExit(
                "post-selector prepare requires --pack-policy and --query-strategy"
            )
        prepare(args)
    else:
        compare(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
