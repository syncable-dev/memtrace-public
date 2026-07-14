#!/usr/bin/env python3
"""Replay ContextBench post-selector packing without touching a live run.

The replay accepts an explicit, sealed task manifest plus prediction and
prediction-audit artifacts. It never reads evaluator output, gold context, a
``LATEST`` pointer, repository source, AWS state, or tasks outside the exact
manifest. Old and current inputs are processed independently with the same
fixed policy and written to a new, content-addressed output bundle.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
from typing import Any, Iterable, Sequence

from post_selector_policy import (
    POLICY,
    POLICY_VERSION,
    PROJECTION_VERSION,
    PackingPolicy,
    PostSelectorPolicyError,
    Span,
    _safe_relative_file,
    policy_fingerprint,
    policy_manifest,
    replay_prediction,
)


ArtifactError = PostSelectorPolicyError


SCHEMA_VERSION = 1
PROHIBITED_PATH_COMPONENTS = {
    "eval",
    "evaluation",
    "evaluations",
    "gold",
    "ground-truth",
    "ground_truth",
    "latest",
    "results",
}


def _explicit_file(value: str | Path, label: str) -> Path:
    unresolved = Path(value).expanduser()
    if not unresolved.is_absolute():
        raise ArtifactError(f"{label} must be an absolute path: {value}")
    if unresolved.is_symlink():
        raise ArtifactError(f"{label} may not be a symlink: {unresolved}")
    path = unresolved.resolve(strict=False)
    components = {
        part.casefold() for part in (*unresolved.parts, *path.parts)
    }
    prohibited = sorted(components & PROHIBITED_PATH_COMPONENTS)
    if prohibited:
        raise ArtifactError(f"{label} contains prohibited path components: {prohibited}")
    if not path.is_file():
        raise ArtifactError(f"{label} must be an existing regular file: {path}")
    return path


def _explicit_directory(value: str | Path, label: str, *, output: bool = False) -> Path:
    unresolved = Path(value).expanduser()
    if not unresolved.is_absolute():
        raise ArtifactError(f"{label} must be an absolute path: {value}")
    if unresolved.is_symlink():
        raise ArtifactError(f"{label} may not be a symlink: {unresolved}")
    path = unresolved.resolve(strict=False)
    components = {
        part.casefold() for part in (*unresolved.parts, *path.parts)
    }
    prohibited = sorted(components & PROHIBITED_PATH_COMPONENTS)
    if prohibited:
        raise ArtifactError(f"{label} contains prohibited path components: {prohibited}")
    if output:
        if path.exists() or path.is_symlink():
            raise ArtifactError(f"output directory already exists and is immutable: {path}")
    elif not path.is_dir() or path.is_symlink():
        raise ArtifactError(f"{label} must be an existing directory: {path}")
    return path


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ArtifactError(f"missing {label}: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ArtifactError(f"invalid {label}: {path}: {error}") from error


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ArtifactError(f"cannot read {label}: {path}: {error}") from error
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ArtifactError(
                f"{label} line {line_number} is invalid JSON: {error}"
            ) from error
        if not isinstance(row, dict):
            raise ArtifactError(f"{label} line {line_number} is not an object")
        rows.append(row)
    return rows


def _manifest_ids(path: Path) -> list[str]:
    loaded = _read_json(path, "sealed IDs manifest")
    items = loaded.get("tasks") if isinstance(loaded, dict) else loaded
    if not isinstance(items, list) or not items:
        raise ArtifactError("sealed IDs manifest must be a non-empty list or tasks[]")
    ids: list[str] = []
    for index, item in enumerate(items):
        value = item.get("instance_id") if isinstance(item, dict) else item
        if value is None or not str(value).strip():
            raise ArtifactError(f"sealed IDs manifest item {index} has no instance_id")
        instance_id = str(value)
        pure = PurePosixPath(instance_id)
        if (
            instance_id in {".", ".."}
            or "\\" in instance_id
            or len(pure.parts) != 1
            or pure.name != instance_id
        ):
            raise ArtifactError(
                f"sealed IDs manifest item {index} is not a safe path segment"
            )
        ids.append(instance_id)
    duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise ArtifactError(f"sealed IDs manifest contains duplicate IDs: {duplicates}")
    return ids


def _index_rows(
    rows: Iterable[dict[str, Any]], ids: Sequence[str], label: str
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for index, row in enumerate(rows):
        instance_id = row.get("instance_id")
        if instance_id is None or not str(instance_id).strip():
            raise ArtifactError(f"{label} row {index} has no instance_id")
        key = str(instance_id)
        if key in indexed:
            duplicates.add(key)
        indexed[key] = row
    if duplicates:
        raise ArtifactError(f"{label} contains duplicate IDs: {sorted(duplicates)}")
    expected = set(ids)
    actual = set(indexed)
    missing = [instance_id for instance_id in ids if instance_id not in actual]
    extras = sorted(actual - expected)
    if missing or extras:
        raise ArtifactError(
            f"{label} IDs differ from sealed manifest; missing={missing}, extras={extras}"
        )
    return indexed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(
        (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode(
            "utf-8"
        )
        for row in rows
    )


def _audit_path(root: Path, instance_id: str) -> Path:
    candidates = (
        root / f"{instance_id}.json",
        root / "prediction-audit" / f"{instance_id}.json",
        root
        / "runs"
        / instance_id
        / "prediction-audit"
        / f"{instance_id}.json",
    )
    matches = []
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        resolved = path.resolve(strict=False)
        if root != resolved and root not in resolved.parents:
            raise ArtifactError(f"sealed prediction audit escapes its root: {path}")
        matches.append(path)
    if len(matches) != 1:
        raise ArtifactError(
            f"expected exactly one sealed prediction audit for {instance_id}; found={matches}"
        )
    return matches[0]


def _load_side(
    label: str,
    predictions_path: Path,
    audit_root: Path,
    ids: Sequence[str],
    *,
    line_budget: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _read_jsonl(predictions_path, f"{label} predictions")
    indexed = _index_rows(rows, ids, f"{label} predictions")
    output_rows: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    input_audits: list[dict[str, Any]] = []
    for instance_id in ids:
        audit_path = _audit_path(audit_root, instance_id)
        audit = _read_json(audit_path, f"{label} prediction audit")
        if not isinstance(audit, dict):
            raise ArtifactError(f"{label} prediction audit is not an object: {audit_path}")
        replayed, replay_audit = replay_prediction(
            indexed[instance_id], audit, line_budget=line_budget
        )
        output_rows.append(replayed)
        audit_rows.append({"instance_id": instance_id, **replay_audit})
        input_audits.append(
            {
                "instance_id": instance_id,
                "path": str(audit_path),
                "sha256": _sha256_file(audit_path),
            }
        )
    return output_rows, audit_rows, input_audits


def replay_pair(
    *,
    ids_manifest: str | Path,
    current_predictions: str | Path,
    current_audits: str | Path,
    old_predictions: str | Path,
    old_audits: str | Path,
    output_dir: str | Path,
    line_budget: int,
) -> dict[str, Any]:
    """Replay old/current sealed artifacts and atomically create a new bundle."""
    manifest_path = _explicit_file(ids_manifest, "sealed IDs manifest")
    current_predictions_path = _explicit_file(
        current_predictions, "current predictions"
    )
    old_predictions_path = _explicit_file(old_predictions, "old predictions")
    current_audit_root = _explicit_directory(current_audits, "current audit root")
    old_audit_root = _explicit_directory(old_audits, "old audit root")
    output = _explicit_directory(output_dir, "output directory", output=True)
    if line_budget < 1:
        raise ArtifactError("line budget must be positive")
    ids = _manifest_ids(manifest_path)

    current_rows, current_replay, current_audit_inputs = _load_side(
        "current",
        current_predictions_path,
        current_audit_root,
        ids,
        line_budget=line_budget,
    )
    old_rows, old_replay, old_audit_inputs = _load_side(
        "old",
        old_predictions_path,
        old_audit_root,
        ids,
        line_budget=line_budget,
    )

    payloads = {
        "current-predictions.jsonl": _jsonl_bytes(current_rows),
        "current-replay-audit.jsonl": _jsonl_bytes(current_replay),
        "old-predictions.jsonl": _jsonl_bytes(old_rows),
        "old-replay-audit.jsonl": _jsonl_bytes(old_replay),
    }
    output_hashes = {
        name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "offline_contextbench_packing_counterfactual",
        "immutable_write_policy": "new_directory_only_atomic_rename",
        "task_count": len(ids),
        "ordered_ids": list(ids),
        "line_budget": line_budget,
        "policy": POLICY.as_dict(),
        "policy_identity": {
            "fingerprint": policy_fingerprint(line_budget=line_budget),
            "manifest": policy_manifest(line_budget=line_budget),
        },
        "inputs": {
            "ids_manifest": {
                "path": str(manifest_path),
                "sha256": _sha256_file(manifest_path),
            },
            "current_predictions": {
                "path": str(current_predictions_path),
                "sha256": _sha256_file(current_predictions_path),
            },
            "old_predictions": {
                "path": str(old_predictions_path),
                "sha256": _sha256_file(old_predictions_path),
            },
            "current_audits": current_audit_inputs,
            "old_audits": old_audit_inputs,
        },
        "outputs": output_hashes,
        "summary": {
            "current": dict(Counter(row["status"] for row in current_replay)),
            "old": dict(Counter(row["status"] for row in old_replay)),
        },
    }
    payloads["manifest.json"] = _json_bytes(manifest)

    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=str(output.parent))
    )
    try:
        for name, payload in payloads.items():
            (stage / name).write_bytes(payload)
        if output.exists() or output.is_symlink():
            raise ArtifactError(f"output directory appeared during write: {output}")
        os.rename(stage, output)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("policy", help="print the immutable packing policy")
    replay = subparsers.add_parser(
        "replay", help="create separate old/current counterfactual predictions"
    )
    replay.add_argument("--ids-manifest", required=True)
    replay.add_argument("--current-predictions", required=True)
    replay.add_argument("--current-audits", required=True)
    replay.add_argument("--old-predictions", required=True)
    replay.add_argument("--old-audits", required=True)
    replay.add_argument("--output-dir", required=True)
    replay.add_argument("--line-budget", required=True, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "policy":
            print(json.dumps(POLICY.as_dict(), indent=2, sort_keys=True))
            return 0
        manifest = replay_pair(
            ids_manifest=args.ids_manifest,
            current_predictions=args.current_predictions,
            current_audits=args.current_audits,
            old_predictions=args.old_predictions,
            old_audits=args.old_audits,
            output_dir=args.output_dir,
            line_budget=args.line_budget,
        )
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    except ArtifactError as error:
        print(f"artifact error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
