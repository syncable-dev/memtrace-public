#!/usr/bin/env python3
"""Fail-closed paired comparison for ContextBench Memtrace artifacts.

The comparator deliberately accepts only explicit artifact directories and
never follows a ``LATEST`` pointer or writes into a source run. Incremental
checkpoints copy completed predictions into immutable 10-task bundles. The
seal command invokes the pinned, unchanged official evaluator only against
those copies, so evaluation can happen off-box without contending with AWS.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import statistics
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 1
FULL_MANIFEST_SIZE = 100
CHECKPOINT_SIZE = 10
BOOTSTRAP_SEED = 20260713
BOOTSTRAP_SAMPLES = 10_000
MIN_LINE_F1_DELTA = 0.03
LANGUAGE_REGRESSION_DELTA = -0.03
LANGUAGE_MIN_CI_TASKS = 5
EPSILON = 1e-12
REPORT_TOLERANCE = 1e-9
TRIAGE_F1_TOLERANCE = 5.1e-5
PINNED_GOLD_SHA256 = "e9dcfd504cbfb849ac815a79040c793d0d92f94eecc9b5a4ee3e1445a2f8a791"
PINNED_CONTEXTBENCH_COMMIT = "1436c28a8eb95496da4ea69ad458b9f8a8eb7d61"
PINNED_PYTHON_VERSION = "3.11.15"
PINNED_DEPENDENCIES = {
    "pyarrow": "25.0.0",
    "datasets": "5.0.0",
    "tree-sitter": "0.20.4",
    "tree-sitter-languages": "1.10.2",
    "numpy": "2.4.6",
    "pandas": "3.0.3",
}
EVALUATOR_HARDENING_ENVIRONMENT = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
}
PAIRED_EVALUATOR_WORKTREE_ISOLATION = {
    "environment_variable": "CONTEXTBENCH_TMP_ROOT",
    "scope": "seal_pair",
    "fresh_root_per_seal": True,
    "shared_between_control_and_candidate": True,
}
REMEDIATION_EVALUATOR_WORKTREE_ISOLATION = {
    "environment_variable": "CONTEXTBENCH_TMP_ROOT",
    "scope": "single_predeclared_false_kill",
    "fresh_root_per_bundle": True,
    "sealed_official_evaluator_receipts": 1,
}
FALSE_KILL_MIN_IMPOSSIBLE_ELAPSED_SECONDS = 365 * 24 * 60 * 60
FALSE_KILL_MAX_RECORDED_RUNNER_SECONDS = 60.0
REMEDIATION_RUN_META_ALLOWED_DIFFERENCES = (
    "run_id",
    "concurrency",
    "manifest_instances",
    "core_pinning.slots",
)
REMEDIATION_PROVENANCE_ALLOWED_DIFFERENCES = (
    "fingerprint",
    "manifest",
    "policy.concurrency",
    "behavior_environment.MEMTRACE_PIN_SLOTS",
    "memtrace.explicit_provenance",
)
REMEDIATION_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
REMEDIATION_TOKEN_MODE = "post-n-100-remediation-token-v1"
REMEDIATION_AUTHORIZATION_MODE = "post-n-100-remediation-authorization-v1"
REMEDIATION_LEDGER_MODE = "append-only-remediation-attempt-ledger-v1"
PACKAGING_RETRY_ATTESTATION_MODE = "remediation-packaging-retry-attestation-v1"
PACKAGING_RETRY_CONSUMPTION_MODE = "remediation-packaging-retry-consumption-v1"
PACKAGING_RETRY_RECEIPT_MODE = "remediation-packaging-retry-evaluation-v1"
PACKAGING_RETRY_INCIDENT_MODE = "post-evaluator-missing-frozen-control-triage-v1"
PACKAGING_RETRY_FAILED_PACKAGER_SHA256 = (
    "463b1a24c1502f205df24d550e163103620c6dd55a1bfd9b320b8fb1e73ce012"
)
PACKAGING_RETRY_EVALUATION_FILES = {
    "prediction.jsonl",
    "result.jsonl",
    "stdout.log",
    "stderr.log",
    "receipt.json",
}
LEGACY_REMEDIATION_POLICY_ATTESTATIONS = {
    "22f2c88f3f5a58ff3312830e9ec41f43be5780d207c780ece93bb99f0c92a1b3": {
        "runner_sha256": (
            "209c88e2517b250b011abfb7c04dba3224e936e6708d1d988ed9ebe358942658"
        ),
        "selector_policy_surface": "absent",
        "post_selector_policy": "off",
    }
}
CANDIDATE_TREATMENT_OFFLINE_PACKING_V2 = "offline-packing-v2"
CANDIDATE_TREATMENT_CHOICES = (CANDIDATE_TREATMENT_OFFLINE_PACKING_V2,)
OFFLINE_PACKING_V2_TREATMENT_CONTRACT = {
    "control_adapter": {
        "driver_bytes": 26445,
        "driver_sha256": (
            "22f2c88f3f5a58ff3312830e9ec41f43be5780d207c780ece93bb99f0c92a1b3"
        ),
        "runner_bytes": 101926,
        "runner_sha256": (
            "209c88e2517b250b011abfb7c04dba3224e936e6708d1d988ed9ebe358942658"
        ),
    },
    "candidate_adapter": {
        "driver_bytes": 31681,
        "driver_sha256": (
            "4a72e3f70e1015e827ea5df2193e475abf9fe34129d70690c474ab3fe428d45b"
        ),
        "runner_bytes": 146918,
        "runner_sha256": (
            "fbc912554f2816193507e77f56ae6f7582ef74ac8220182fa4a2dd47f630d390"
        ),
    },
    "post_selector_implementation_sha256": (
        "cbb3b03988c6219a8cf50840df641363dcea2d26e684b95464a91b3b798ba1c4"
    ),
    "post_selector_fingerprint": (
        "a0c4d74d4463caad1eda75f4c068e008f4eeb64ea67418bcdef5c879e0ebd516"
    ),
    "line_budget": 200,
    "selector_policy": "replay-v1",
}
OFFLINE_PACKING_V2_RUN_META_DIFFERENCE_PATHS = {
    "manifest_source_run_id",
    "post_selector_policy",
}
OFFLINE_PACKING_V2_PROVENANCE_DIFFERENCE_PATHS = {
    "behavior_environment.CB_POST_SELECTOR_POLICY",
    "driver.bytes",
    "driver.sha256",
    "policy.post_selector_identity",
    "policy.post_selector_policy",
    "policy.selector_policy",
    "runner.bytes",
    "runner.sha256",
}
IMPORT_AFFECTING_IGNORED_SUFFIXES = {
    ".py",
    ".pyc",
    ".pyo",
    ".pth",
    ".egg-link",
    ".so",
    ".pyd",
    ".dylib",
}
FAILURE_HASH_FIELDS = ("audit_sha256", "query_plan_sha256", "failure_sha256")
LEGACY_FAILURE_RECORD_KEYS = {
    "schema_version",
    "status",
    "instance_id",
    "run_fingerprint",
    "failure_kind",
    "prediction_sha256",
}
TERMINAL_IDENTITY_FIELDS = (
    "status",
    "run_record_mtime_ns",
    "run_record_sha256",
    "prediction_sha256",
    "audit_sha256",
    "query_plan_sha256",
    "failure_sha256",
)

GRANULARITIES = ("file", "symbol", "line")
REPORT_LABEL = {"file": "file", "symbol": "block", "line": "line"}
REQUIRED_FULL_FILES = (
    "manifest.json",
    "run_meta.json",
    "run_provenance.json",
    "predictions.jsonl",
    "results.jsonl",
    "evaluation-failures.jsonl",
    "leaderboard-report.json",
    "driver_summary.json",
    "watchdog.log",
    "triage/triage.jsonl",
    "triage/triage_summary.json",
)
REQUIRED_CHECKPOINT_FILES = (
    "manifest.json",
    "batch-manifest.json",
    "run_meta.json",
    "run_provenance.json",
    "predictions.jsonl",
    "results.jsonl",
    "evaluation-failures.jsonl",
    "leaderboard-report.json",
    "checkpoint.json",
    "completion-records.json",
    "triage/triage.jsonl",
    "triage/triage_summary.json",
)

EXPECTED_METRIC_CONTRACT = {
    "aggregation": "macro average over the manifest task universe",
    "failure_accounting": (
        "harness failures, evaluator errors, and explicitly allowed missing "
        "artifacts contribute zero recall, precision, F1, and efficiency"
    ),
    "block_definition": "definition-level AST symbols",
    "pass_at_1": "test-passing patch rate; null for retrieval-only runs",
}


class ArtifactError(ValueError):
    """An input artifact is missing, inconsistent, or not publishable."""


def explicit_path(value: str | Path, label: str, *, must_exist: bool = True) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ArtifactError(f"{label} must be an absolute path: {value}")
    if any(part.casefold() == "latest" for part in path.parts):
        raise ArtifactError(f"{label} may not contain a LATEST path component")
    path = path.resolve(strict=False)
    if must_exist and not path.is_dir():
        raise ArtifactError(f"{label} is not an existing directory: {path}")
    return path


def read_json(path: Path, label: str | None = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ArtifactError(f"missing {label or path.name}: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ArtifactError(f"invalid {label or path.name}: {path}: {error}") from error


def read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ArtifactError(f"cannot read {label}: {path}: {error}") from error
    rows: list[dict[str, Any]] = []
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
            raise ArtifactError(f"{label} line {line_number} is not a JSON object")
        rows.append(row)
    return rows


def manifest_ids(path: Path, *, expected_count: int | None = None) -> list[str]:
    loaded = read_json(path, "manifest")
    items = loaded.get("tasks") if isinstance(loaded, dict) else loaded
    if not isinstance(items, list):
        raise ArtifactError("manifest must be a JSON list or an object with tasks[]")
    ids: list[str] = []
    for index, item in enumerate(items):
        value = item.get("instance_id") if isinstance(item, dict) else item
        if value is None or not str(value).strip():
            raise ArtifactError(f"manifest item {index} has no instance_id")
        ids.append(str(value))
    duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise ArtifactError(f"manifest contains duplicate IDs: {duplicates}")
    if expected_count is not None and len(ids) != expected_count:
        raise ArtifactError(
            f"manifest must contain exactly {expected_count} IDs; found {len(ids)}"
        )
    if not ids:
        raise ArtifactError("manifest is empty")
    return ids


def index_exact_rows(
    rows: Iterable[dict[str, Any]],
    expected_ids: Sequence[str],
    label: str,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for index, row in enumerate(rows):
        value = row.get("instance_id")
        if value is None or not str(value).strip():
            raise ArtifactError(f"{label} row {index} has no exact instance_id")
        instance_id = str(value)
        if instance_id in indexed:
            duplicates.add(instance_id)
        indexed[instance_id] = row
    if duplicates:
        raise ArtifactError(f"{label} contains duplicate IDs: {sorted(duplicates)}")
    expected = set(expected_ids)
    actual = set(indexed)
    missing = [instance_id for instance_id in expected_ids if instance_id not in actual]
    extras = sorted(actual - expected)
    if missing or extras:
        raise ArtifactError(
            f"{label} IDs differ from the manifest; missing={missing}, extras={extras}"
        )
    return indexed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(
        (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        for row in rows
    )


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve(strict=False)
    right = right.resolve(strict=False)
    return left == right or left in right.parents or right in left.parents


def reject_path_overlap(
    path: Path,
    label: str,
    protected: Sequence[tuple[str, Path]],
) -> None:
    for protected_label, protected_path in protected:
        if paths_overlap(path, protected_path):
            raise ArtifactError(
                f"{label} overlaps protected {protected_label}: {protected_path}"
            )


def write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(path.parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        else:
            fsync_directory(path.parent)
        raise


def write_exclusive_group(files: Sequence[tuple[Path, bytes]]) -> None:
    created: list[Path] = []
    try:
        for path, payload in files:
            write_exclusive(path, payload)
            created.append(path)
    except BaseException:
        for path in reversed(created):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise


def read_jsonl_exact(path: Path, label: str) -> list[dict[str, Any]]:
    rows = read_jsonl(path, label)
    if not rows:
        raise ArtifactError(f"{label} is empty")
    return rows


def require_files(root: Path, names: Sequence[str]) -> None:
    missing = [name for name in names if not (root / name).is_file()]
    if missing:
        raise ArtifactError(f"{root} is missing required artifacts: {missing}")


def _drop_keys(value: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    return {key: copy.deepcopy(item) for key, item in value.items() if key not in keys}


def _difference_endpoint(value: Any, present: bool) -> dict[str, Any]:
    endpoint: dict[str, Any] = {"present": present}
    if present:
        endpoint["value"] = copy.deepcopy(value)
    return endpoint


def _json_difference_map(
    control: Any,
    candidate: Any,
    prefix: str = "",
) -> dict[str, dict[str, Any]]:
    """Return every raw JSON difference without conflating missing with null."""
    if isinstance(control, dict) and isinstance(candidate, dict):
        differences: dict[str, dict[str, Any]] = {}
        for key in sorted(set(control) | set(candidate)):
            path = f"{prefix}.{key}" if prefix else str(key)
            control_present = key in control
            candidate_present = key in candidate
            if not control_present or not candidate_present:
                differences[path] = {
                    "control": _difference_endpoint(
                        control.get(key), control_present
                    ),
                    "candidate": _difference_endpoint(
                        candidate.get(key), candidate_present
                    ),
                }
                continue
            differences.update(
                _json_difference_map(control[key], candidate[key], path)
            )
        return differences
    if control == candidate:
        return {}
    return {
        prefix: {
            "control": _difference_endpoint(control, True),
            "candidate": _difference_endpoint(candidate, True),
        }
    }


def _identity_matches_contract(
    identity: Any,
    contract: dict[str, Any],
    prefix: str,
) -> bool:
    return bool(
        isinstance(identity, dict)
        and identity.get("bytes") == contract[f"{prefix}_bytes"]
        and identity.get("sha256") == contract[f"{prefix}_sha256"]
    )


def _validate_offline_packing_v2_treatment(
    control_meta: dict[str, Any],
    candidate_meta: dict[str, Any],
    control_provenance: dict[str, Any],
    candidate_provenance: dict[str, Any],
    meta_differences: dict[str, dict[str, Any]],
    provenance_differences: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if set(meta_differences) != OFFLINE_PACKING_V2_RUN_META_DIFFERENCE_PATHS:
        raise ArtifactError(
            "offline-packing-v2 treatment has unexpected run_meta differences: "
            f"{sorted(meta_differences)}"
        )
    if set(provenance_differences) != OFFLINE_PACKING_V2_PROVENANCE_DIFFERENCE_PATHS:
        raise ArtifactError(
            "offline-packing-v2 treatment has unexpected run provenance differences: "
            f"{sorted(provenance_differences)}"
        )

    name = CANDIDATE_TREATMENT_OFFLINE_PACKING_V2
    contract = OFFLINE_PACKING_V2_TREATMENT_CONTRACT
    if (
        not isinstance(control_meta.get("manifest_source_run_id"), str)
        or not control_meta["manifest_source_run_id"]
        or not isinstance(candidate_meta.get("manifest_source_run_id"), str)
        or not candidate_meta["manifest_source_run_id"]
        or "post_selector_policy" in control_meta
        or control_meta.get("line_budget") != contract["line_budget"]
        or candidate_meta.get("post_selector_policy") != name
        or candidate_meta.get("line_budget") != contract["line_budget"]
    ):
        raise ArtifactError("offline-packing-v2 run_meta treatment contract failed")

    control_driver = control_provenance.get("driver")
    control_runner = control_provenance.get("runner")
    candidate_driver = candidate_provenance.get("driver")
    candidate_runner = candidate_provenance.get("runner")
    if not _identity_matches_contract(
        control_driver, contract["control_adapter"], "driver"
    ) or not _identity_matches_contract(
        control_runner, contract["control_adapter"], "runner"
    ):
        raise ArtifactError(
            "offline-packing-v2 control adapter is not the pinned legacy adapter"
        )
    if not _identity_matches_contract(
        candidate_driver, contract["candidate_adapter"], "driver"
    ) or not _identity_matches_contract(
        candidate_runner, contract["candidate_adapter"], "runner"
    ):
        raise ArtifactError(
            "offline-packing-v2 candidate adapter is not the pinned treatment adapter"
        )

    control_environment = control_provenance.get("behavior_environment")
    candidate_environment = candidate_provenance.get("behavior_environment")
    control_policy = control_provenance.get("policy")
    candidate_policy = candidate_provenance.get("policy")
    if not all(
        isinstance(value, dict)
        for value in (
            control_environment,
            candidate_environment,
            control_policy,
            candidate_policy,
        )
    ):
        raise ArtifactError("offline-packing-v2 treatment provenance is malformed")
    assert isinstance(control_environment, dict)
    assert isinstance(candidate_environment, dict)
    assert isinstance(control_policy, dict)
    assert isinstance(candidate_policy, dict)
    if (
        "CB_POST_SELECTOR_POLICY" in control_environment
        or candidate_environment.get("CB_POST_SELECTOR_POLICY") != name
        or "selector_policy" in control_policy
        or "post_selector_policy" in control_policy
        or "post_selector_identity" in control_policy
        or candidate_policy.get("selector_policy") != contract["selector_policy"]
        or candidate_policy.get("post_selector_policy") != name
        or candidate_policy.get("line_budget") != contract["line_budget"]
    ):
        raise ArtifactError("offline-packing-v2 policy treatment contract failed")

    identity = candidate_policy.get("post_selector_identity")
    if not isinstance(identity, dict):
        raise ArtifactError("offline-packing-v2 candidate has no bound policy identity")
    manifest = identity.get("manifest")
    fingerprint = identity.get("fingerprint")
    implementation = manifest.get("implementation") if isinstance(manifest, dict) else None
    runtime = manifest.get("runtime_parameters") if isinstance(manifest, dict) else None
    policy = manifest.get("policy") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or fingerprint != contract["post_selector_fingerprint"]
        or canonical_sha256(manifest) != fingerprint
        or not isinstance(implementation, dict)
        or implementation.get("module") != "post_selector_policy.py"
        or implementation.get("sha256")
        != contract["post_selector_implementation_sha256"]
        or runtime
        != {
            "line_budget": contract["line_budget"],
            "strict_runtime_audit": True,
        }
        or not isinstance(policy, dict)
        or policy.get("version") != name
    ):
        raise ArtifactError("offline-packing-v2 policy identity binding failed")

    return {
        "name": name,
        "contract": copy.deepcopy(contract),
        "exact_differences": {
            "run_meta": copy.deepcopy(meta_differences),
            "run_provenance": copy.deepcopy(provenance_differences),
        },
    }


def validate_parity(
    control_root: Path,
    candidate_root: Path,
    candidate_treatment: str | None = None,
) -> dict[str, Any]:
    if (
        candidate_treatment is not None
        and candidate_treatment not in CANDIDATE_TREATMENT_CHOICES
    ):
        raise ArtifactError(f"unsupported candidate treatment: {candidate_treatment}")
    control_meta = read_json(control_root / "run_meta.json")
    candidate_meta = read_json(candidate_root / "run_meta.json")
    if not isinstance(control_meta, dict) or not isinstance(candidate_meta, dict):
        raise ArtifactError("run_meta.json must be a JSON object")
    allowed_meta = {"run_id", "cache_namespace", "memtrace_source"}
    meta_differences = _json_difference_map(
        _drop_keys(control_meta, allowed_meta),
        _drop_keys(candidate_meta, allowed_meta),
    )
    if candidate_treatment is None and meta_differences:
        raise ArtifactError(
            "run_meta parity failed; only run_id, cache_namespace, and "
            "memtrace_source may differ"
        )

    control_provenance = read_json(control_root / "run_provenance.json")
    candidate_provenance = read_json(candidate_root / "run_provenance.json")
    if not isinstance(control_provenance, dict) or not isinstance(candidate_provenance, dict):
        raise ArtifactError("run_provenance.json must be a JSON object")
    control_normalized = _drop_keys(control_provenance, {"fingerprint", "memtrace"})
    candidate_normalized = _drop_keys(candidate_provenance, {"fingerprint", "memtrace"})
    for normalized in (control_normalized, candidate_normalized):
        manifest = normalized.get("manifest")
        if isinstance(manifest, dict):
            manifest.pop("path", None)
    provenance_differences = _json_difference_map(
        control_normalized,
        candidate_normalized,
    )
    if candidate_treatment is None and provenance_differences:
        raise ArtifactError(
            "run provenance parity failed; only fingerprint, manifest.path, and "
            "the memtrace source/binary section may differ"
        )
    treatment = None
    if candidate_treatment == CANDIDATE_TREATMENT_OFFLINE_PACKING_V2:
        treatment = _validate_offline_packing_v2_treatment(
            control_meta,
            candidate_meta,
            control_provenance,
            candidate_provenance,
            meta_differences,
            provenance_differences,
        )
    return {
        "run_meta_allowed_differences": sorted(allowed_meta),
        "run_provenance_allowed_differences": [
            "fingerprint",
            "manifest.path",
            "memtrace",
        ],
        "policy": copy.deepcopy(control_provenance.get("policy")),
        "candidate_treatment": treatment,
    }


def harmonic_mean(recall: float, precision: float) -> float:
    return 0.0 if recall + precision == 0 else 2 * recall * precision / (recall + precision)


def finite_unit(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArtifactError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ArtifactError(f"{label} must be finite and between 0 and 1")
    return result


def task_metrics(
    instance_id: str,
    prediction: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, dict[str, float]]:
    failure = prediction.get("harness_failure")
    if failure is not None and not isinstance(failure, dict):
        raise ArtifactError(f"prediction {instance_id} has invalid harness_failure")
    trajectory = prediction.get("traj_data")
    if not isinstance(trajectory, dict) or not isinstance(trajectory.get("pred_steps"), list):
        raise ArtifactError(f"prediction {instance_id} has invalid traj_data.pred_steps")
    zeroed = bool(failure) or bool(result.get("error"))
    metrics: dict[str, dict[str, float]] = {}
    for granularity in GRANULARITIES:
        if zeroed:
            recall = precision = 0.0
        else:
            metric = result.get("final", {}).get(granularity)
            if not isinstance(metric, dict):
                raise ArtifactError(f"result {instance_id} has no final.{granularity}")
            recall = finite_unit(metric.get("coverage"), f"{instance_id}.{granularity}.coverage")
            precision = finite_unit(
                metric.get("precision"), f"{instance_id}.{granularity}.precision"
            )
        metrics[granularity] = {
            "recall": recall,
            "precision": precision,
            "f1": harmonic_mean(recall, precision),
        }
    return metrics


def macro_metrics(
    ids: Sequence[str],
    per_task: dict[str, dict[str, dict[str, float]]],
) -> dict[str, dict[str, float]]:
    if not ids:
        raise ArtifactError("cannot compute metrics for an empty ID set")
    return {
        granularity: {
            field: statistics.fmean(per_task[instance_id][granularity][field] for instance_id in ids)
            for field in ("recall", "precision", "f1")
        }
        for granularity in GRANULARITIES
    }


def close(actual: Any, expected: float, tolerance: float = REPORT_TOLERANCE) -> bool:
    return isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isclose(
        float(actual), expected, rel_tol=0.0, abs_tol=tolerance
    )


def validate_report(
    report: dict[str, Any],
    ids: Sequence[str],
    predictions: dict[str, dict[str, Any]],
    results: dict[str, dict[str, Any]],
    macros: dict[str, dict[str, float]],
) -> None:
    leaderboard = report.get("leaderboard")
    completeness = report.get("completeness")
    contract = report.get("metric_contract")
    if not isinstance(leaderboard, dict) or not isinstance(completeness, dict):
        raise ArtifactError("leaderboard report lacks leaderboard/completeness objects")
    for key, expected in EXPECTED_METRIC_CONTRACT.items():
        if not isinstance(contract, dict) or contract.get(key) != expected:
            raise ArtifactError(f"metric contract mismatch for {key}")
    if leaderboard.get("pass_at_1") is not None:
        raise ArtifactError("Pass@1 must remain null for the retrieval-only benchmark")
    for granularity in GRANULARITIES:
        reported = leaderboard.get(REPORT_LABEL[granularity])
        if not isinstance(reported, dict):
            raise ArtifactError(f"leaderboard lacks {REPORT_LABEL[granularity]} metrics")
        for field in ("recall", "precision", "f1"):
            if not close(reported.get(field), macros[granularity][field]):
                raise ArtifactError(
                    f"stale leaderboard {REPORT_LABEL[granularity]}.{field}: "
                    f"reported={reported.get(field)!r}, recomputed={macros[granularity][field]}"
                )

    failures = {
        instance_id
        for instance_id in ids
        if bool(predictions[instance_id].get("harness_failure"))
    }
    errors = {instance_id for instance_id in ids if bool(results[instance_id].get("error"))}
    zero_ids = failures | errors
    exact = {
        "manifest_instances": len(ids),
        "predictions": len(ids),
        "evaluation_results": len(ids),
        "harness_failure_predictions": len(failures),
        "evaluator_error_results": len(errors),
        "zero_accounted_instances": len(zero_ids),
        "pass_results": 0,
        "allow_partial": False,
        "artifact_complete": True,
        "source_artifact_complete": True,
        "score_publishable": True,
    }
    for key, expected in exact.items():
        if completeness.get(key) != expected:
            raise ArtifactError(
                f"completeness.{key} is {completeness.get(key)!r}; expected {expected!r}"
            )
    for key in (
        "missing_predictions",
        "invalid_predictions",
        "missing_evaluation_results",
        "invalid_evaluation_results",
        "evaluation_reconciled_failure_predictions",
        "missing_pass_results",
    ):
        if completeness.get(key) != []:
            raise ArtifactError(f"completeness.{key} must be empty")
    reasons = completeness.get("zero_accounted_reasons")
    if not isinstance(reasons, dict) or set(reasons) != zero_ids:
        raise ArtifactError("zero-accounted failure IDs do not match failures/evaluator errors")


def validate_triage(
    ids: Sequence[str],
    triage: dict[str, dict[str, Any]],
    summary: dict[str, Any],
    per_task: dict[str, dict[str, dict[str, float]]],
) -> dict[str, str]:
    languages: dict[str, str] = {}
    layers: Counter[str] = Counter()
    by_language: dict[str, Counter[str]] = defaultdict(Counter)
    for instance_id in ids:
        row = triage[instance_id]
        language = row.get("language")
        layer = row.get("primary")
        if not isinstance(language, str) or not language:
            raise ArtifactError(f"triage {instance_id} has no language")
        if not isinstance(layer, str) or not layer:
            raise ArtifactError(f"triage {instance_id} has no primary layer")
        languages[instance_id] = language
        layers[layer] += 1
        by_language[language][layer] += 1
        row_metrics = row.get("metrics")
        if not isinstance(row_metrics, dict):
            raise ArtifactError(f"triage {instance_id} has no metrics")
        for granularity in GRANULARITIES:
            metric = row_metrics.get(granularity)
            expected = per_task[instance_id][granularity]
            if not isinstance(metric, dict):
                raise ArtifactError(f"triage {instance_id} lacks {granularity} metrics")
            for field in ("recall", "precision"):
                if not close(metric.get(field), expected[field]):
                    raise ArtifactError(
                        f"triage {instance_id} {granularity}.{field} disagrees with evaluator"
                    )
            if not close(metric.get("f1"), expected["f1"], TRIAGE_F1_TOLERANCE):
                raise ArtifactError(
                    f"triage {instance_id} {granularity}.f1 disagrees with evaluator"
                )
    if summary.get("num_tasks") != len(ids):
        raise ArtifactError("triage summary num_tasks does not match manifest")
    expected_line = round(
        statistics.fmean(per_task[instance_id]["line"]["f1"] for instance_id in ids), 4
    )
    if not close(summary.get("macro_line_f1"), expected_line, TRIAGE_F1_TOLERANCE):
        raise ArtifactError("triage summary macro_line_f1 is stale")
    if summary.get("layer_counts") != dict(layers):
        raise ArtifactError("triage summary layer_counts disagree with triage rows")
    expected_by_language = {
        language: dict(counts) for language, counts in sorted(by_language.items())
    }
    if summary.get("by_language") != expected_by_language:
        raise ArtifactError("triage summary by_language disagrees with triage rows")
    if summary.get("untriaged_instances", []) != []:
        raise ArtifactError("triage summary contains untriaged instances")
    budget_check = summary.get("budget_check")
    if isinstance(budget_check, dict) and budget_check.get("ok") is not True:
        raise ArtifactError("triage budget check is not OK")
    return languages


def validate_evaluation_failures(
    root: Path,
    ids: Sequence[str],
    predictions: dict[str, dict[str, Any]],
) -> set[str]:
    rows = read_jsonl(root / "evaluation-failures.jsonl", "evaluation failures")
    indexed: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        instance_id = row.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            raise ArtifactError(f"evaluation failures row {index} has no instance_id")
        if instance_id in indexed:
            raise ArtifactError(f"evaluation failures contains duplicate ID {instance_id}")
        if instance_id not in set(ids):
            raise ArtifactError(f"evaluation failures contains ID outside manifest: {instance_id}")
        indexed[instance_id] = row
    expected = {
        instance_id
        for instance_id in ids
        if bool(predictions[instance_id].get("harness_failure"))
    }
    if set(indexed) != expected:
        raise ArtifactError(
            "evaluation-failures IDs do not exactly match harness-failure predictions"
        )
    return expected


def percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def wall_stats(records: dict[str, dict[str, Any]], wall_clock: Any) -> dict[str, Any]:
    seconds = [
        float(record["seconds"])
        for record in records.values()
        if isinstance(record, dict)
        and isinstance(record.get("seconds"), (int, float))
        and not isinstance(record.get("seconds"), bool)
        and math.isfinite(float(record["seconds"]))
        and float(record["seconds"]) >= 0
    ]
    return {
        "task_seconds": {
            "count": len(seconds),
            "p50": percentile(seconds, 0.50),
            "p95": percentile(seconds, 0.95),
            "max": max(seconds) if seconds else None,
        },
        "run_wall_clock_seconds": (
            float(wall_clock)
            if isinstance(wall_clock, (int, float))
            and not isinstance(wall_clock, bool)
            and math.isfinite(float(wall_clock))
            else None
        ),
    }


def timeout_ids_from_log(path: Path, universe: set[str]) -> set[str]:
    timeouts: set[str] = set()
    if not path.is_file():
        return timeouts
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "timeout" not in line.casefold():
            continue
        match = re.search(r"(?:slug|instance_id)=([^\s]+)", line)
        if match and match.group(1) in universe:
            timeouts.add(match.group(1))
    return timeouts


def reliability_from_records(
    ids: Sequence[str],
    predictions: dict[str, dict[str, Any]],
    results: dict[str, dict[str, Any]],
    records: dict[str, dict[str, Any]],
    watchdog_path: Path | None,
) -> dict[str, Any]:
    failures = {
        instance_id
        for instance_id in ids
        if bool(predictions[instance_id].get("harness_failure"))
        or records.get(instance_id, {}).get("status") == "failure"
    }
    evaluator_errors = {instance_id for instance_id in ids if bool(results[instance_id].get("error"))}
    timeouts = timeout_ids_from_log(watchdog_path, set(ids)) if watchdog_path else set()
    for instance_id in ids:
        record = records.get(instance_id, {})
        prediction_failure = predictions[instance_id].get("harness_failure") or {}
        kinds = (
            str(record.get("failure_kind", "")),
            str(prediction_failure.get("kind", "")) if isinstance(prediction_failure, dict) else "",
            str(prediction_failure.get("message", "")) if isinstance(prediction_failure, dict) else "",
        )
        if any("timeout" in value.casefold() for value in kinds):
            timeouts.add(instance_id)
    if not timeouts <= failures:
        raise ArtifactError("timeout IDs must be a subset of harness/driver failures")
    return {
        "failures": len(failures),
        "failure_ids": sorted(failures),
        "evaluator_errors": len(evaluator_errors),
        "evaluator_error_ids": sorted(evaluator_errors),
        "timeouts": len(timeouts),
        "timeout_ids": sorted(timeouts),
    }


def validate_driver_summary(
    summary: dict[str, Any],
    ids: Sequence[str],
    predictions: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], Any]:
    records = summary.get("per_instance")
    if not isinstance(records, dict):
        raise ArtifactError("driver summary has no per_instance object")
    if set(records) != set(ids):
        raise ArtifactError("driver summary per_instance IDs differ from the manifest")
    for instance_id, record in records.items():
        if not isinstance(record, dict) or record.get("status") not in {"success", "failure"}:
            raise ArtifactError(f"driver summary record {instance_id} has invalid status")
    failures = {
        instance_id
        for instance_id in ids
        if records[instance_id].get("status") == "failure"
    }
    prediction_failures = {
        instance_id
        for instance_id in ids
        if bool(predictions[instance_id].get("harness_failure"))
    }
    if failures != prediction_failures:
        raise ArtifactError("driver failure IDs differ from harness-failure predictions")
    successes = len(ids) - len(failures)
    expected = {
        "total": len(ids),
        "prediction_records": len(ids),
        "completed": successes,
        "failed": len(failures),
    }
    for key, value in expected.items():
        if summary.get(key) != value:
            raise ArtifactError(f"driver_summary.{key} must be {value}")
    return records, summary.get("wall_clock_seconds")


def verify_frozen_checkpoint(root: Path) -> dict[str, Any]:
    checkpoint = read_json(root / "checkpoint.json")
    if not isinstance(checkpoint, dict) or checkpoint.get("schema_version") != SCHEMA_VERSION:
        raise ArtifactError("invalid checkpoint.json schema")
    frozen = checkpoint.get("frozen_files")
    if not isinstance(frozen, dict) or not frozen:
        raise ArtifactError("checkpoint.json has no frozen_files hashes")
    for name, expected_hash in frozen.items():
        if not isinstance(name, str) or not isinstance(expected_hash, str):
            raise ArtifactError("checkpoint frozen_files entries must be string hashes")
        path = root / name
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise ArtifactError(f"immutable checkpoint file changed: {name}")
    return checkpoint


def validate_checkpoint_candidate_treatment(
    checkpoint: dict[str, Any],
    requested_treatment: str | None = None,
    *,
    expected_parity: dict[str, Any] | None = None,
    require_explicit: bool = False,
) -> dict[str, Any] | None:
    """Validate and, when requested, explicitly acknowledge a frozen treatment."""
    parity = checkpoint.get("parity")
    if parity is not None and not isinstance(parity, dict):
        raise ArtifactError("checkpoint parity contract is malformed")
    if expected_parity is not None and parity != expected_parity:
        raise ArtifactError("checkpoint parity differs from the requested candidate treatment")

    treatment = parity.get("candidate_treatment") if isinstance(parity, dict) else None
    if treatment is not None:
        if (
            not isinstance(treatment, dict)
            or treatment.get("name") not in CANDIDATE_TREATMENT_CHOICES
            or treatment.get("contract") != OFFLINE_PACKING_V2_TREATMENT_CONTRACT
        ):
            raise ArtifactError("checkpoint candidate treatment contract is malformed")
        exact_differences = treatment.get("exact_differences")
        run_meta_differences = (
            exact_differences.get("run_meta")
            if isinstance(exact_differences, dict)
            else None
        )
        provenance_differences = (
            exact_differences.get("run_provenance")
            if isinstance(exact_differences, dict)
            else None
        )
        if (
            not isinstance(exact_differences, dict)
            or not isinstance(run_meta_differences, dict)
            or not isinstance(provenance_differences, dict)
            or set(run_meta_differences)
            != OFFLINE_PACKING_V2_RUN_META_DIFFERENCE_PATHS
            or set(provenance_differences)
            != OFFLINE_PACKING_V2_PROVENANCE_DIFFERENCE_PATHS
        ):
            raise ArtifactError("checkpoint candidate treatment differences are malformed")

    stored_name = treatment.get("name") if isinstance(treatment, dict) else None
    if requested_treatment is not None and requested_treatment not in CANDIDATE_TREATMENT_CHOICES:
        raise ArtifactError(f"unsupported candidate treatment: {requested_treatment}")
    if requested_treatment != stored_name and (require_explicit or requested_treatment is not None):
        if stored_name is not None and requested_treatment is None:
            raise ArtifactError(
                f"checkpoint requires explicit --candidate-treatment {stored_name}"
            )
        raise ArtifactError("requested candidate treatment differs from the frozen checkpoint")
    return copy.deepcopy(treatment)


def load_run(
    root: Path,
    *,
    expected_count: int,
    checkpoint: bool = False,
) -> dict[str, Any]:
    require_files(root, REQUIRED_CHECKPOINT_FILES if checkpoint else REQUIRED_FULL_FILES)
    checkpoint_meta = verify_frozen_checkpoint(root) if checkpoint else None
    ids = manifest_ids(root / "manifest.json", expected_count=expected_count)
    predictions = index_exact_rows(
        read_jsonl(root / "predictions.jsonl", "predictions"), ids, "predictions"
    )
    results = index_exact_rows(read_jsonl(root / "results.jsonl", "results"), ids, "results")
    triage = index_exact_rows(
        read_jsonl(root / "triage/triage.jsonl", "triage"), ids, "triage"
    )
    validate_evaluation_failures(root, ids, predictions)
    per_task = {
        instance_id: task_metrics(instance_id, predictions[instance_id], results[instance_id])
        for instance_id in ids
    }
    macros = macro_metrics(ids, per_task)
    report = read_json(root / "leaderboard-report.json")
    if not isinstance(report, dict):
        raise ArtifactError("leaderboard-report.json must be an object")
    validate_report(report, ids, predictions, results, macros)
    triage_summary = read_json(root / "triage/triage_summary.json")
    if not isinstance(triage_summary, dict):
        raise ArtifactError("triage summary must be an object")
    languages = validate_triage(ids, triage, triage_summary, per_task)

    if checkpoint:
        completion = read_json(root / "completion-records.json")
        records = completion.get("per_instance") if isinstance(completion, dict) else None
        if not isinstance(records, dict) or set(records) != set(ids):
            raise ArtifactError("checkpoint completion records IDs differ from manifest")
        wall_clock = completion.get("source_wall_clock_seconds")
        watchdog = root / "watchdog.log"
    else:
        driver = read_json(root / "driver_summary.json")
        if not isinstance(driver, dict):
            raise ArtifactError("driver_summary.json must be an object")
        records, wall_clock = validate_driver_summary(driver, ids, predictions)
        watchdog = root / "watchdog.log"

    reliability = reliability_from_records(
        ids, predictions, results, records, watchdog if watchdog.is_file() else None
    )
    return {
        "root": root,
        "ids": ids,
        "predictions": predictions,
        "results": results,
        "triage": triage,
        "languages": languages,
        "per_task": per_task,
        "macros": macros,
        "report": report,
        "records": records,
        "reliability": reliability,
        "wall_clock": wall_stats(records, wall_clock),
        "checkpoint": checkpoint_meta,
    }


def paired_bootstrap(deltas: Sequence[float]) -> dict[str, Any]:
    if not deltas:
        raise ArtifactError("paired bootstrap needs at least one task")
    rng = random.Random(BOOTSTRAP_SEED)
    size = len(deltas)
    draws = [
        statistics.fmean(deltas[rng.randrange(size)] for _ in range(size))
        for _ in range(BOOTSTRAP_SAMPLES)
    ]
    return {
        "seed": BOOTSTRAP_SEED,
        "samples": BOOTSTRAP_SAMPLES,
        "method": "paired task bootstrap, percentile interval",
        "mean_delta": statistics.fmean(deltas),
        "ci_95": {
            "lower": percentile(draws, 0.025),
            "upper": percentile(draws, 0.975),
        },
    }


def compare_metric_set(
    ids: Sequence[str],
    control: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    control_macros = macro_metrics(ids, control["per_task"])
    candidate_macros = macro_metrics(ids, candidate["per_task"])
    deltas: dict[str, dict[str, float]] = {}
    movement: dict[str, dict[str, int]] = {}
    for granularity in GRANULARITIES:
        deltas[granularity] = {
            field: candidate_macros[granularity][field] - control_macros[granularity][field]
            for field in ("recall", "precision", "f1")
        }
        counts = {"improved": 0, "regressed": 0, "tied": 0}
        for instance_id in ids:
            difference = (
                candidate["per_task"][instance_id][granularity]["f1"]
                - control["per_task"][instance_id][granularity]["f1"]
            )
            if difference > EPSILON:
                counts["improved"] += 1
            elif difference < -EPSILON:
                counts["regressed"] += 1
            else:
                counts["tied"] += 1
        movement[granularity] = counts
    f1_bootstraps = {
        granularity: paired_bootstrap(
            [
                candidate["per_task"][instance_id][granularity]["f1"]
                - control["per_task"][instance_id][granularity]["f1"]
                for instance_id in ids
            ]
        )
        for granularity in GRANULARITIES
    }
    return {
        "count": len(ids),
        "control": control_macros,
        "candidate": candidate_macros,
        "delta": deltas,
        "movement": movement,
        "f1_bootstrap": f1_bootstraps,
        "line_f1_bootstrap": f1_bootstraps["line"],
    }


def per_language_comparison(
    ids: Sequence[str],
    control: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for instance_id in ids:
        control_language = control["languages"][instance_id]
        candidate_language = candidate["languages"][instance_id]
        if control_language != candidate_language:
            raise ArtifactError(f"language changed for {instance_id}")
        grouped[control_language].append(instance_id)
    output: dict[str, Any] = {}
    for language, language_ids in sorted(grouped.items()):
        deltas = [
            candidate["per_task"][instance_id]["line"]["f1"]
            - control["per_task"][instance_id]["line"]["f1"]
            for instance_id in language_ids
        ]
        bootstrap = paired_bootstrap(deltas)
        delta = statistics.fmean(deltas)
        meaningful = delta <= LANGUAGE_REGRESSION_DELTA + EPSILON
        significant = (
            len(language_ids) >= LANGUAGE_MIN_CI_TASKS
            and bootstrap["ci_95"]["upper"] < 0.0
        )
        output[language] = {
            "count": len(language_ids),
            "control_line_f1": statistics.fmean(
                control["per_task"][instance_id]["line"]["f1"]
                for instance_id in language_ids
            ),
            "candidate_line_f1": statistics.fmean(
                candidate["per_task"][instance_id]["line"]["f1"]
                for instance_id in language_ids
            ),
            "delta": delta,
            "bootstrap": bootstrap,
            "meaningful_regression": meaningful,
            "statistically_significant_regression": significant,
            "gate_passed": not (meaningful or significant),
        }
    return output


def subset_reliability(run: dict[str, Any], ids: Sequence[str]) -> dict[str, Any]:
    failures = {
        instance_id
        for instance_id in ids
        if instance_id in set(run["reliability"]["failure_ids"])
    }
    errors = {
        instance_id
        for instance_id in ids
        if instance_id in set(run["reliability"]["evaluator_error_ids"])
    }
    timeouts = {
        instance_id
        for instance_id in ids
        if instance_id in set(run["reliability"]["timeout_ids"])
    }
    return {
        "failures": len(failures),
        "failure_ids": sorted(failures),
        "evaluator_errors": len(errors),
        "evaluator_error_ids": sorted(errors),
        "timeouts": len(timeouts),
        "timeout_ids": sorted(timeouts),
    }


def release_gate(
    comparison: dict[str, Any],
    languages: dict[str, Any],
    control_reliability: dict[str, Any],
    candidate_reliability: dict[str, Any],
) -> dict[str, Any]:
    ci_lower = comparison["line_f1_bootstrap"]["ci_95"]["lower"]
    checks = {
        "file_f1_no_regression": comparison["delta"]["file"]["f1"] >= -EPSILON,
        "symbol_f1_no_regression": comparison["delta"]["symbol"]["f1"] >= -EPSILON,
        "line_f1_no_regression": comparison["delta"]["line"]["f1"] >= -EPSILON,
        "line_f1_delta_at_least_0_03": (
            comparison["delta"]["line"]["f1"] >= MIN_LINE_F1_DELTA - EPSILON
        ),
        "paired_line_f1_ci_lower_above_zero": ci_lower is not None and ci_lower > 0.0,
        "failure_count_not_increased": (
            candidate_reliability["failures"] <= control_reliability["failures"]
        ),
        "timeout_count_not_increased": (
            candidate_reliability["timeouts"] <= control_reliability["timeouts"]
        ),
        "evaluator_error_count_not_increased": (
            candidate_reliability["evaluator_errors"]
            <= control_reliability["evaluator_errors"]
        ),
        "no_meaningful_or_significant_language_regression": all(
            row["gate_passed"] for row in languages.values()
        ),
        "pass_at_1_separated_and_null": True,
    }
    reasons = [name for name, passed in checks.items() if not passed]
    return {
        "passed": not reasons,
        "checks": checks,
        "failure_reasons": reasons,
        "policy": {
            "minimum_line_f1_delta": MIN_LINE_F1_DELTA,
            "paired_ci_requirement": "95% percentile CI lower bound > 0",
            "language_meaningful_regression_delta": LANGUAGE_REGRESSION_DELTA,
            "language_significant_regression": (
                f"n >= {LANGUAGE_MIN_CI_TASKS} and paired CI upper bound < 0"
            ),
            "pass_at_1": None,
        },
    }


def per_task_output(
    ids: Sequence[str], control: dict[str, Any], candidate: dict[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for instance_id in ids:
        rows.append(
            {
                "instance_id": instance_id,
                "language": control["languages"][instance_id],
                "control": copy.deepcopy(control["per_task"][instance_id]),
                "candidate": copy.deepcopy(candidate["per_task"][instance_id]),
                "delta_f1": {
                    granularity: (
                        candidate["per_task"][instance_id][granularity]["f1"]
                        - control["per_task"][instance_id][granularity]["f1"]
                    )
                    for granularity in GRANULARITIES
                },
            }
        )
    return rows


def full_comparison(
    control_root: Path,
    candidate_root: Path,
    candidate_treatment: str | None = None,
) -> dict[str, Any]:
    control_manifest = control_root / "manifest.json"
    candidate_manifest = candidate_root / "manifest.json"
    if control_manifest.read_bytes() != candidate_manifest.read_bytes():
        raise ArtifactError("control and candidate manifest bytes are not identical")
    parity = validate_parity(
        control_root,
        candidate_root,
        candidate_treatment,
    )
    control = load_run(control_root, expected_count=FULL_MANIFEST_SIZE)
    candidate = load_run(candidate_root, expected_count=FULL_MANIFEST_SIZE)
    if control["ids"] != candidate["ids"]:
        raise ArtifactError("control and candidate manifest ID order differs")
    ids = control["ids"]
    comparison = compare_metric_set(ids, control, candidate)
    languages = per_language_comparison(ids, control, candidate)
    gate = release_gate(
        comparison, languages, control["reliability"], candidate["reliability"]
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "full",
        "inputs": {
            "control": str(control_root),
            "candidate": str(candidate_root),
            "candidate_treatment": candidate_treatment,
        },
        "validation": {
            "manifest_byte_identical": True,
            "manifest_instances": len(ids),
            "unique_exact_ids": True,
            "current_reports_recomputed": True,
            "triage_agreement": True,
            "zero_accounted_failures": True,
            "pass_at_1": None,
            "parity": parity,
        },
        "comparison": comparison,
        "per_language": languages,
        "reliability": {
            "control": control["reliability"],
            "candidate": candidate["reliability"],
        },
        "wall_clock": {
            "control": control["wall_clock"],
            "candidate": candidate["wall_clock"],
        },
        "gate": gate,
        "per_task": per_task_output(ids, control, candidate),
    }


def legacy_failure_attestation(
    instance_id: str,
    fingerprint: str,
    record: dict[str, Any],
    prediction: dict[str, Any],
    audit: dict[str, Any],
    failure: dict[str, Any],
    audit_hash: str,
    failure_hash: str,
    query_plan_path: Path,
) -> dict[str, Any]:
    if set(record) != LEGACY_FAILURE_RECORD_KEYS:
        raise ArtifactError(f"legacy failure run-record shape is not exact: {instance_id}")
    if query_plan_path.exists():
        raise ArtifactError(f"legacy failure unexpectedly has an unbound query plan: {instance_id}")
    trajectory = prediction.get("traj_data")
    expected_trajectory = {
        "pred_steps": [],
        "pred_files": [],
        "pred_spans": {},
        "pred_symbols": {},
    }
    if (
        set(prediction) != {"instance_id", "traj_data", "model_patch", "harness_failure"}
        or trajectory != expected_trajectory
        or prediction.get("model_patch") != ""
    ):
        raise ArtifactError(f"legacy failure prediction is not an exact zero stub: {instance_id}")
    prediction_failure = prediction.get("harness_failure")
    expected_prediction_failure = {
        "kind": failure.get("kind"),
        "message": failure.get("message"),
        "run_fingerprint": fingerprint,
    }
    if prediction_failure != expected_prediction_failure:
        raise ArtifactError(f"legacy failure prediction identity disagrees: {instance_id}")
    if audit != {"harness_failure": failure}:
        raise ArtifactError(f"legacy failure audit is not an exact failure copy: {instance_id}")
    returncode = failure.get("returncode")
    seconds = failure.get("seconds")
    timeout_seconds = failure.get("timeout_seconds")
    command = failure.get("command")
    if (
        failure.get("schema_version") != 1
        or failure.get("instance_id") != instance_id
        or failure.get("run_fingerprint") != fingerprint
        or failure.get("kind") != "nonzero_exit"
        or record.get("failure_kind") != failure.get("kind")
        or isinstance(returncode, bool)
        or not isinstance(returncode, int)
        or returncode == 0
        or failure.get("message") != f"runner exited with return code {returncode}"
        or isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(float(seconds))
        or float(seconds) < 0.0
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or float(timeout_seconds) <= 0.0
        or float(seconds) > float(timeout_seconds)
        or not isinstance(command, list)
        or any(not isinstance(part, str) for part in command)
        or command.count("--instance-id") != 1
        or command.index("--instance-id") + 1 >= len(command)
        or command[command.index("--instance-id") + 1] != instance_id
        or not isinstance(failure.get("log"), str)
        or not failure["log"]
    ):
        raise ArtifactError(f"legacy failure semantics are not coherent: {instance_id}")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "legacy_failure_semantic_attestation_v1",
        "legacy_run_record_keys": sorted(LEGACY_FAILURE_RECORD_KEYS),
        "missing_run_record_hash_fields": list(FAILURE_HASH_FIELDS),
        "run_record_prediction_hash_verified": True,
        "exact_zero_prediction_stub_verified": True,
        "audit_exactly_matches_failure": True,
        "query_plan_absent": True,
        "failure_kind": failure["kind"],
        "failure_returncode": returncode,
        "failure_seconds": seconds,
        "frozen_audit_sha256": audit_hash,
        "frozen_failure_sha256": failure_hash,
        "frozen_query_plan_sha256": None,
    }


def terminal_records(candidate_root: Path, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    runs_root = candidate_root / "runs"
    if not runs_root.is_dir():
        raise ArtifactError(f"candidate snapshot has no runs directory: {runs_root}")
    provenance = read_json(candidate_root / "run_provenance.json")
    fingerprint = provenance.get("fingerprint") if isinstance(provenance, dict) else None
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ArtifactError("candidate run provenance has no fingerprint")
    universe = set(ids)
    indexed: dict[str, dict[str, Any]] = {}
    for record_path in sorted(runs_root.glob("*/run_record.json")):
        record = read_json(record_path, "run record")
        if not isinstance(record, dict):
            raise ArtifactError(f"run record is not an object: {record_path}")
        instance_id = record.get("instance_id")
        status = record.get("status")
        if instance_id not in universe:
            raise ArtifactError(f"run record contains an ID outside the manifest: {instance_id}")
        if status not in {"success", "failure"}:
            raise ArtifactError(f"run record has a non-terminal or malformed status: {instance_id}")
        if instance_id in indexed:
            raise ArtifactError(f"multiple terminal run records for {instance_id}")
        if record.get("run_fingerprint") != fingerprint:
            raise ArtifactError(f"terminal run record has stale fingerprint: {instance_id}")
        prediction_path = record_path.parent / "prediction.jsonl"
        rows = read_jsonl(prediction_path, f"prediction for {instance_id}")
        if len(rows) != 1 or rows[0].get("instance_id") != instance_id:
            raise ArtifactError(f"terminal prediction is not one exact row: {instance_id}")
        prediction_hash = sha256_file(prediction_path)
        if record.get("prediction_sha256") != prediction_hash:
            raise ArtifactError(f"terminal prediction hash mismatch: {instance_id}")
        audit_path = record_path.parent / "prediction-audit" / f"{record_path.parent.name}.json"
        if not audit_path.is_file():
            raise ArtifactError(f"terminal task has no per-task audit: {instance_id}")
        audit = read_json(audit_path, f"audit for {instance_id}")
        if not isinstance(audit, dict):
            raise ArtifactError(f"terminal audit is not an object: {instance_id}")
        audit_hash = sha256_file(audit_path)
        query_plan_path = record_path.parent / "query-plan.json"
        failure_path = record_path.parent / "failure.json"
        failure: dict[str, Any] = {}
        validation_mode = "hash_bound_terminal_v1"
        legacy_attestation: dict[str, Any] | None = None
        if status == "failure":
            loaded_failure = read_json(failure_path, f"failure for {instance_id}")
            if not isinstance(loaded_failure, dict) or loaded_failure.get("instance_id") != instance_id:
                raise ArtifactError(f"terminal failure record is invalid: {instance_id}")
            failure = loaded_failure
            failure_hash = sha256_file(failure_path)
            query_plan_hash = (
                sha256_file(query_plan_path) if query_plan_path.is_file() else None
            )
            present_hash_fields = {field for field in FAILURE_HASH_FIELDS if field in record}
            if present_hash_fields == set(FAILURE_HASH_FIELDS):
                if record.get("audit_sha256") != audit_hash:
                    raise ArtifactError(f"terminal failure audit hash mismatch: {instance_id}")
                if record.get("failure_sha256") != failure_hash:
                    raise ArtifactError(f"terminal failure artifact hash mismatch: {instance_id}")
                if record.get("query_plan_sha256") != query_plan_hash:
                    raise ArtifactError(f"terminal failure query-plan hash mismatch: {instance_id}")
            elif present_hash_fields:
                raise ArtifactError(
                    f"terminal failure has a partial hash binding: {instance_id}"
                )
            else:
                validation_mode = "legacy_failure_semantic_attestation_v1"
                legacy_attestation = legacy_failure_attestation(
                    instance_id,
                    fingerprint,
                    record,
                    rows[0],
                    audit,
                    failure,
                    audit_hash,
                    failure_hash,
                    query_plan_path,
                )
            if (
                failure.get("schema_version") != 1
                or failure.get("run_fingerprint") != fingerprint
                or record.get("failure_kind") != failure.get("kind")
            ):
                raise ArtifactError(f"terminal failure identity disagrees: {instance_id}")
            if not rows[0].get("harness_failure"):
                raise ArtifactError(f"failed terminal prediction is not zero-accounted: {instance_id}")
            audit_failure = audit.get("harness_failure")
            prediction_failure = rows[0].get("harness_failure")
            if (
                not isinstance(audit_failure, dict)
                or audit_failure.get("instance_id") != instance_id
                or audit_failure.get("kind") != failure.get("kind")
                or audit_failure != failure
                or not isinstance(prediction_failure, dict)
                or prediction_failure.get("kind") != failure.get("kind")
                or prediction_failure.get("run_fingerprint") != fingerprint
            ):
                raise ArtifactError(f"failed terminal audit identity disagrees: {instance_id}")
        elif rows[0].get("harness_failure"):
            raise ArtifactError(f"successful terminal prediction has harness_failure: {instance_id}")
        else:
            if record.get("audit_sha256") != audit_hash:
                raise ArtifactError(f"terminal audit hash mismatch: {instance_id}")
            if not query_plan_path.is_file():
                raise ArtifactError(f"successful terminal task has no query plan: {instance_id}")
            if record.get("query_plan_sha256") != sha256_file(query_plan_path):
                raise ArtifactError(f"terminal query-plan hash mismatch: {instance_id}")
        stat = record_path.stat()
        indexed[instance_id] = {
            "instance_id": instance_id,
            "status": status,
            "seconds": record.get("seconds", failure.get("seconds")),
            "failure_kind": record.get("failure_kind", failure.get("kind")),
            "run_record_mtime_ns": stat.st_mtime_ns,
            "run_record_sha256": sha256_file(record_path),
            "prediction_sha256": prediction_hash,
            "audit_sha256": audit_hash,
            "query_plan_sha256": (
                sha256_file(query_plan_path) if query_plan_path.is_file() else None
            ),
                "failure_sha256": sha256_file(failure_path) if failure_path.is_file() else None,
            "validation_mode": validation_mode,
            "legacy_failure_attestation": legacy_attestation,
            "prediction_path": prediction_path,
            "run_record_path": record_path,
            "audit_path": audit_path,
            "query_plan_path": query_plan_path if query_plan_path.is_file() else None,
            "failure_path": failure_path if failure_path.is_file() else None,
            "prediction": rows[0],
        }
    return indexed


def checkpoint_directories(root: Path) -> list[Path]:
    directories = [
        path
        for path in root.glob("checkpoint-[0-9][0-9][0-9][0-9]")
        if path.is_dir()
    ]
    return sorted(directories, key=lambda path: path.name)


def validate_frozen_legacy_attestation(instance_id: str, record: dict[str, Any]) -> None:
    attestation = record.get("legacy_failure_attestation")
    if (
        record.get("status") != "failure"
        or record.get("validation_mode") != "legacy_failure_semantic_attestation_v1"
        or not isinstance(attestation, dict)
        or attestation.get("schema_version") != SCHEMA_VERSION
        or attestation.get("mode") != "legacy_failure_semantic_attestation_v1"
        or attestation.get("legacy_run_record_keys") != sorted(LEGACY_FAILURE_RECORD_KEYS)
        or attestation.get("missing_run_record_hash_fields") != list(FAILURE_HASH_FIELDS)
        or attestation.get("run_record_prediction_hash_verified") is not True
        or attestation.get("exact_zero_prediction_stub_verified") is not True
        or attestation.get("audit_exactly_matches_failure") is not True
        or attestation.get("query_plan_absent") is not True
        or attestation.get("frozen_audit_sha256") != record.get("audit_sha256")
        or attestation.get("frozen_failure_sha256") != record.get("failure_sha256")
        or attestation.get("frozen_query_plan_sha256") is not None
        or record.get("query_plan_sha256") is not None
    ):
        raise ArtifactError(f"frozen legacy failure attestation is invalid: {instance_id}")


def load_checkpoint_chain(root: Path) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    prior_ids: list[str] = []
    prior_hash: str | None = None
    for index, directory in enumerate(checkpoint_directories(root)):
        count = (index + 1) * CHECKPOINT_SIZE
        if directory.name != f"checkpoint-{count:04d}":
            raise ArtifactError("checkpoint sequence has a gap or unexpected directory")
        meta = verify_frozen_checkpoint(directory)
        ids = manifest_ids(directory / "manifest.json", expected_count=count)
        batch_ids = manifest_ids(directory / "batch-manifest.json", expected_count=CHECKPOINT_SIZE)
        if ids[: len(prior_ids)] != prior_ids or ids[-CHECKPOINT_SIZE:] != batch_ids:
            raise ArtifactError(f"checkpoint manifest chain is inconsistent at N={count}")
        if meta.get("completed_count") != count or meta.get("batch_ids") != batch_ids:
            raise ArtifactError(f"checkpoint metadata is inconsistent at N={count}")
        if meta.get("parent_checkpoint_sha256") != prior_hash:
            raise ArtifactError(f"checkpoint parent hash is inconsistent at N={count}")
        completion = read_json(directory / "completion-records.json")
        records = completion.get("per_instance") if isinstance(completion, dict) else None
        if (
            not isinstance(records, dict)
            or set(records) != set(ids)
            or completion.get("ordered_ids") != ids
        ):
            raise ArtifactError(f"checkpoint completion order disagrees at N={count}")
        attested_ids = [
            instance_id
            for instance_id in ids
            if records[instance_id].get("validation_mode")
            == "legacy_failure_semantic_attestation_v1"
        ]
        if meta.get("legacy_failure_attestation_ids", []) != attested_ids:
            raise ArtifactError(f"checkpoint legacy failure attestations disagree at N={count}")
        for instance_id in attested_ids:
            validate_frozen_legacy_attestation(instance_id, records[instance_id])
        source_manifest = manifest_ids(directory / "source-manifest.json", expected_count=100)
        source_order = {instance_id: index for index, instance_id in enumerate(source_manifest)}
        ordering = [
            (int(records[instance_id]["run_record_mtime_ns"]), source_order[instance_id])
            for instance_id in ids
        ]
        if ordering != sorted(ordering):
            raise ArtifactError(f"checkpoint terminal ordering is not sealed at N={count}")
        chain.append(
            {
                "dir": directory,
                "meta": meta,
                "ids": ids,
                "batch_ids": batch_ids,
                "completion_records": records,
            }
        )
        prior_ids = ids
        prior_hash = sha256_file(directory / "checkpoint.json")
    return chain


def checkpoint_source_reconciliation(checkpoint_dir: Path | None) -> dict[str, Any]:
    """Load the durable audit of source drift without requiring it on old checkpoints."""
    empty = {
        "schema_version": SCHEMA_VERSION,
        "frozen_completion_archive_authoritative": True,
        "events": [],
    }
    if checkpoint_dir is None:
        return empty
    path = checkpoint_dir / "source-reconciliation.json"
    if not path.is_file():
        return empty
    loaded = read_json(path, "checkpoint source reconciliation")
    if (
        not isinstance(loaded, dict)
        or loaded.get("schema_version") != SCHEMA_VERSION
        or loaded.get("frozen_completion_archive_authoritative") is not True
        or not isinstance(loaded.get("events"), list)
        or any(not isinstance(event, dict) for event in loaded["events"])
    ):
        raise ArtifactError("checkpoint source reconciliation has an invalid schema")
    return loaded


def reconcile_frozen_source(
    already_ids: Sequence[str],
    frozen_records: dict[str, dict[str, Any]],
    current_terminals: dict[str, dict[str, Any]],
    prior_reconciliation: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Keep frozen terminal identities authoritative and audit later source drift."""
    current_events: list[dict[str, Any]] = []
    for index, instance_id in enumerate(already_ids):
        frozen = frozen_records[instance_id]
        frozen_identity = {field: frozen.get(field) for field in TERMINAL_IDENTITY_FIELDS}
        current = current_terminals.get(instance_id)
        if current is None:
            current_events.append(
                {
                    "instance_id": instance_id,
                    "kind": "missing_from_source_snapshot",
                    "first_frozen_checkpoint": ((index // CHECKPOINT_SIZE) + 1)
                    * CHECKPOINT_SIZE,
                    "frozen": frozen_identity,
                    "current": None,
                    "differing_fields": list(TERMINAL_IDENTITY_FIELDS),
                }
            )
            continue
        current_identity = {field: current.get(field) for field in TERMINAL_IDENTITY_FIELDS}
        differing = [
            field
            for field in TERMINAL_IDENTITY_FIELDS
            if frozen_identity[field] != current_identity[field]
        ]
        if differing:
            current_events.append(
                {
                    "instance_id": instance_id,
                    "kind": "source_terminal_changed_after_freeze",
                    "first_frozen_checkpoint": ((index // CHECKPOINT_SIZE) + 1)
                    * CHECKPOINT_SIZE,
                    "frozen": frozen_identity,
                    "current": current_identity,
                    "differing_fields": differing,
                }
            )

    historical = list(prior_reconciliation["events"])
    seen = {
        json.dumps(event, sort_keys=True, separators=(",", ":")) for event in historical
    }
    for event in current_events:
        identity = json.dumps(event, sort_keys=True, separators=(",", ":"))
        if identity not in seen:
            historical.append(event)
            seen.add(identity)
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "frozen_completion_archive_authoritative": True,
            "events": historical,
        },
        current_events,
    )


def _selected_watchdog_lines(path: Path, ids: set[str]) -> bytes:
    if not path.is_file():
        return b""
    selected: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.search(r"(?:slug|instance_id)=([^\s]+)", line)
        if match and match.group(1) in ids:
            selected.append(line)
    return (("\n".join(selected) + "\n") if selected else "").encode("utf-8")


def _checkpoint_payload(
    control_root: Path,
    candidate_root: Path,
    cumulative_ids: list[str],
    batch_ids: list[str],
    candidate_predictions: dict[str, dict[str, Any]],
    control_predictions: dict[str, dict[str, Any]],
    control_results: dict[str, dict[str, Any]],
    control_triage: dict[str, dict[str, Any]],
    completion_records: dict[str, dict[str, Any]],
    source_reconciliation: dict[str, Any],
) -> dict[str, bytes]:
    cumulative_manifest = json_bytes(cumulative_ids)
    batch_manifest = json_bytes(batch_ids)
    source_files: list[str] = ["manifest.json", "run_meta.json", "run_provenance.json"]
    for instance_id in batch_ids:
        record = completion_records[instance_id]
        for key in (
            "run_record_relative_path",
            "prediction_relative_path",
            "audit_relative_path",
            "query_plan_relative_path",
        ):
            value = record.get(key)
            if isinstance(value, str):
                source_files.append(value)
        for key in ("failure_relative_path",):
            value = record.get(key)
            if isinstance(value, str):
                source_files.append(value)
    completion = {
        "schema_version": SCHEMA_VERSION,
        "source_wall_clock_seconds": None,
        "ordered_ids": cumulative_ids,
        "per_instance": {
            instance_id: completion_records[instance_id] for instance_id in cumulative_ids
        },
    }
    payload = {
        "manifest.json": cumulative_manifest,
        "control-manifest.json": cumulative_manifest,
        "batch-manifest.json": batch_manifest,
        "control-batch-manifest.json": batch_manifest,
        "source-manifest.json": (candidate_root / "manifest.json").read_bytes(),
        "candidate-predictions.jsonl": jsonl_bytes(
            candidate_predictions[instance_id] for instance_id in cumulative_ids
        ),
        "control-predictions.jsonl": jsonl_bytes(
            control_predictions[instance_id] for instance_id in cumulative_ids
        ),
        "candidate-batch-predictions.jsonl": jsonl_bytes(
            candidate_predictions[instance_id] for instance_id in batch_ids
        ),
        "control-batch-predictions.jsonl": jsonl_bytes(
            control_predictions[instance_id] for instance_id in batch_ids
        ),
        "control-stored-results.jsonl": jsonl_bytes(
            control_results[instance_id] for instance_id in cumulative_ids
        ),
        "control-batch-stored-results.jsonl": jsonl_bytes(
            control_results[instance_id] for instance_id in batch_ids
        ),
        "control-triage.jsonl": jsonl_bytes(control_triage[instance_id] for instance_id in cumulative_ids),
        "completion-records.json": json_bytes(completion),
        "source-reconciliation.json": json_bytes(source_reconciliation),
        "run_meta.json": (candidate_root / "run_meta.json").read_bytes(),
        "run_provenance.json": (candidate_root / "run_provenance.json").read_bytes(),
        "watchdog.log": _selected_watchdog_lines(candidate_root / "watchdog.log", set(cumulative_ids)),
        "source-files.txt": ("\n".join(dict.fromkeys(source_files)) + "\n").encode("utf-8"),
    }
    for instance_id in batch_ids:
        record = completion_records[instance_id]
        for key in (
            "run_record_relative_path",
            "prediction_relative_path",
            "audit_relative_path",
            "query_plan_relative_path",
            "failure_relative_path",
        ):
            relative = record.get(key)
            if isinstance(relative, str):
                payload[f"source-artifacts/{relative}"] = (candidate_root / relative).read_bytes()
    return payload


def freeze_checkpoints(
    control_root: Path,
    candidate_root: Path,
    checkpoint_root: Path,
    candidate_treatment: str | None = None,
) -> dict[str, Any]:
    if checkpoint_root == candidate_root or candidate_root in checkpoint_root.parents:
        raise ArtifactError("checkpoint root may not be the candidate run or live beneath it")
    if checkpoint_root == control_root or control_root in checkpoint_root.parents:
        raise ArtifactError("checkpoint root may not be the control run or live beneath it")
    require_files(
        control_root,
        ("manifest.json", "predictions.jsonl", "results.jsonl", "triage/triage.jsonl"),
    )
    require_files(candidate_root, ("manifest.json", "run_meta.json", "run_provenance.json"))
    if (control_root / "manifest.json").read_bytes() != (candidate_root / "manifest.json").read_bytes():
        raise ArtifactError("control and candidate source manifest bytes are not identical")
    ids = manifest_ids(candidate_root / "manifest.json", expected_count=FULL_MANIFEST_SIZE)
    if manifest_ids(control_root / "manifest.json", expected_count=FULL_MANIFEST_SIZE) != ids:
        raise ArtifactError("control and candidate source manifest order differs")
    parity = validate_parity(
        control_root,
        candidate_root,
        candidate_treatment,
    )
    control_predictions = index_exact_rows(
        read_jsonl(control_root / "predictions.jsonl", "control predictions"),
        ids,
        "control predictions",
    )
    control_results = index_exact_rows(
        read_jsonl(control_root / "results.jsonl", "control stored results"),
        ids,
        "control stored results",
    )
    control_triage = index_exact_rows(
        read_jsonl(control_root / "triage/triage.jsonl", "control triage"),
        ids,
        "control triage",
    )
    terminals = terminal_records(candidate_root, ids)
    manifest_order = {instance_id: index for index, instance_id in enumerate(ids)}
    ordered_terminal_ids = sorted(
        terminals,
        key=lambda instance_id: (
            terminals[instance_id]["run_record_mtime_ns"],
            manifest_order[instance_id],
        ),
    )

    checkpoint_root.mkdir(parents=True, exist_ok=True)
    chain = load_checkpoint_chain(checkpoint_root)
    already_ids = chain[-1]["ids"] if chain else []
    if len(already_ids) != len(set(already_ids)):
        raise ArtifactError("existing checkpoint chain repeats IDs")
    existing_completion: dict[str, dict[str, Any]] = {}
    existing_candidate_predictions: dict[str, dict[str, Any]] = {}
    if chain:
        last_dir = chain[-1]["dir"]
        completion = read_json(last_dir / "completion-records.json")
        existing_completion = completion.get("per_instance", {}) if isinstance(completion, dict) else {}
        if set(existing_completion) != set(already_ids):
            raise ArtifactError("existing checkpoint completion records disagree with manifest")
        existing_candidate_predictions = index_exact_rows(
            read_jsonl(last_dir / "candidate-predictions.jsonl", "checkpoint candidate predictions"),
            already_ids,
            "checkpoint candidate predictions",
        )
        source_manifest_hash = sha256_file(candidate_root / "manifest.json")
        if chain[-1]["meta"].get("source_manifest_sha256") != source_manifest_hash:
            raise ArtifactError("candidate source manifest changed since the last checkpoint")

    already_set = set(already_ids)
    pending = [instance_id for instance_id in ordered_terminal_ids if instance_id not in already_set]
    seconds = Counter(
        int(terminals[instance_id]["run_record_mtime_ns"]) // 1_000_000_000
        for instance_id in pending
    )
    ambiguous_seconds = sorted(second for second, occurrences in seconds.items() if occurrences > 1)
    if ambiguous_seconds:
        raise ArtifactError(
            "terminal run_record ordering is ambiguous after snapshot transfer: multiple "
            "unfrozen records share a whole-second mtime; recapture with authoritative remote "
            "nanosecond mtimes because the comparator refuses to guess the completion order"
        )
    if already_ids and pending:
        last_record = existing_completion[already_ids[-1]]
        last_key = (
            int(last_record["run_record_mtime_ns"]),
            manifest_order[already_ids[-1]],
        )
        first_pending_key = (
            int(terminals[pending[0]]["run_record_mtime_ns"]),
            manifest_order[pending[0]],
        )
        if first_pending_key < last_key:
            raise ArtifactError(
                "a newly visible terminal record predates the frozen ordering; refresh the "
                "include-only snapshot before freezing"
            )

    new_checkpoint_dirs: list[str] = []
    cumulative_ids = list(already_ids)
    cumulative_predictions = dict(existing_candidate_predictions)
    completion_records = dict(existing_completion)
    source_manifest_hash = sha256_file(candidate_root / "manifest.json")
    candidate_fingerprint = read_json(candidate_root / "run_provenance.json").get("fingerprint")
    if not isinstance(candidate_fingerprint, str) or not candidate_fingerprint:
        raise ArtifactError("candidate source has no run fingerprint")
    for checkpoint in chain:
        if checkpoint["meta"].get("source_manifest_sha256") != source_manifest_hash:
            raise ArtifactError("existing checkpoint chain uses a different source manifest")
        if checkpoint["meta"].get("candidate_run_fingerprint") != candidate_fingerprint:
            raise ArtifactError("existing checkpoint chain mixes candidate run fingerprints")
        stored_parity = checkpoint["meta"].get("parity")
        if candidate_treatment is not None and stored_parity != parity:
            raise ArtifactError(
                "existing checkpoint chain uses a different candidate-treatment contract"
            )
        if (
            candidate_treatment is None
            and isinstance(stored_parity, dict)
            and stored_parity.get("candidate_treatment") is not None
        ):
            raise ArtifactError(
                "existing checkpoint chain requires an explicit candidate treatment"
            )
    prior_reconciliation = checkpoint_source_reconciliation(
        chain[-1]["dir"] if chain else None
    )
    source_reconciliation, current_source_drift = reconcile_frozen_source(
        already_ids,
        existing_completion,
        terminals,
        prior_reconciliation,
    )
    parent_hash = sha256_file(chain[-1]["dir"] / "checkpoint.json") if chain else None
    complete_batch_count = len(pending) // CHECKPOINT_SIZE
    for batch_index in range(complete_batch_count):
        batch_ids = pending[
            batch_index * CHECKPOINT_SIZE : (batch_index + 1) * CHECKPOINT_SIZE
        ]
        for instance_id in batch_ids:
            terminal = terminals[instance_id]
            cumulative_ids.append(instance_id)
            cumulative_predictions[instance_id] = terminal["prediction"]
            record_relative = terminal["run_record_path"].relative_to(candidate_root)
            prediction_relative = terminal["prediction_path"].relative_to(candidate_root)
            audit_relative = terminal["audit_path"].relative_to(candidate_root)
            query_plan_relative = (
                terminal["query_plan_path"].relative_to(candidate_root)
                if terminal["query_plan_path"] is not None
                else None
            )
            failure_relative = (
                terminal["failure_path"].relative_to(candidate_root)
                if terminal["failure_path"] is not None
                else None
            )
            completion_records[instance_id] = {
                "status": terminal["status"],
                "seconds": terminal["seconds"],
                "failure_kind": terminal["failure_kind"],
                "run_record_mtime_ns": terminal["run_record_mtime_ns"],
                "run_record_sha256": terminal["run_record_sha256"],
                "prediction_sha256": terminal["prediction_sha256"],
                "audit_sha256": terminal["audit_sha256"],
                "query_plan_sha256": terminal["query_plan_sha256"],
                "failure_sha256": terminal["failure_sha256"],
                "validation_mode": terminal["validation_mode"],
                "legacy_failure_attestation": terminal["legacy_failure_attestation"],
                "run_record_relative_path": str(record_relative),
                "prediction_relative_path": str(prediction_relative),
                "audit_relative_path": str(audit_relative),
                "query_plan_relative_path": (
                    str(query_plan_relative) if query_plan_relative is not None else None
                ),
                "failure_relative_path": (
                    str(failure_relative) if failure_relative is not None else None
                ),
            }
        count = len(cumulative_ids)
        target = checkpoint_root / f"checkpoint-{count:04d}"
        if target.exists():
            raise ArtifactError(f"refusing to overwrite checkpoint: {target}")
        payload = _checkpoint_payload(
            control_root,
            candidate_root,
            cumulative_ids,
            batch_ids,
            cumulative_predictions,
            control_predictions,
            control_results,
            control_triage,
            completion_records,
            source_reconciliation,
        )
        frozen_hashes = {
            name: hashlib.sha256(data).hexdigest() for name, data in payload.items()
        }
        checkpoint_meta = {
            "schema_version": SCHEMA_VERSION,
            "completed_count": count,
            "checkpoint_size": CHECKPOINT_SIZE,
            "batch_ids": batch_ids,
            "ordering": (
                "run_record.json mtime_ns; original manifest order tie-break; snapshots "
                "without authoritative remote ordering must have unique whole-second mtimes"
            ),
            "ordering_transport_requirement": (
                "the snapshot copy must preserve nanosecond mtimes; plain rsync -t may "
                "truncate precision on some filesystems, so validate st_mtime_ns after transfer"
            ),
            "includes_failures": True,
            "source_control": str(control_root),
            "source_candidate": str(candidate_root),
            "source_manifest_sha256": source_manifest_hash,
            "candidate_run_fingerprint": candidate_fingerprint,
            "parity": parity,
            "parent_checkpoint_sha256": parent_hash,
            "frozen_completion_archive_authoritative": True,
            "source_reconciliation_event_count": len(source_reconciliation["events"]),
            "current_source_drift_count": len(current_source_drift),
            "legacy_failure_attestation_ids": [
                instance_id
                for instance_id in cumulative_ids
                if completion_records[instance_id].get("validation_mode")
                == "legacy_failure_semantic_attestation_v1"
            ],
            "frozen_files": frozen_hashes,
            "evaluator_contract": {
                "scope": "evaluate both control and candidate only for batch-manifest.json",
                "environment": {"SYMBOL_DETAIL_MAX": "0"},
                "mutation_boundary": "evaluator outputs may be sealed here; source runs are read-only",
            },
        }
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=checkpoint_root))
        try:
            for name, data in payload.items():
                write_bytes(temporary / name, data)
            write_bytes(temporary / "checkpoint.json", json_bytes(checkpoint_meta))
            os.replace(temporary, target)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        parent_hash = sha256_file(target / "checkpoint.json")
        new_checkpoint_dirs.append(str(target))

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "freeze-checkpoints",
        "inputs": {
            "control": str(control_root),
            "candidate": str(candidate_root),
            "checkpoint_root": str(checkpoint_root),
            "candidate_treatment": candidate_treatment,
        },
        "terminal_tasks": len(terminals),
        "already_frozen": len(already_ids),
        "newly_frozen": len(new_checkpoint_dirs) * CHECKPOINT_SIZE,
        "pending_terminal_tasks": len(pending) % CHECKPOINT_SIZE,
        "new_checkpoints": new_checkpoint_dirs,
        "source_reconciliation": {
            "frozen_completion_archive_authoritative": True,
            "current_source_drift": current_source_drift,
            "historical_event_count": len(source_reconciliation["events"]),
        },
        "legacy_failure_attestation_ids": [
            instance_id
            for instance_id in cumulative_ids
            if completion_records[instance_id].get("validation_mode")
            == "legacy_failure_semantic_attestation_v1"
        ],
        "parity": parity,
        "source_run_mutated": False,
    }


def explicit_file(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ArtifactError(f"{label} must be an absolute path: {value}")
    if any(part.casefold() == "latest" for part in path.parts):
        raise ArtifactError(f"{label} may not contain a LATEST path component")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ArtifactError(f"{label} is not a file: {path}")
    return path


def evaluator_execution_environment(runtime: dict[str, Any]) -> dict[str, str]:
    environment = dict(runtime["environment"])
    environment.update(EVALUATOR_HARDENING_ENVIRONMENT)
    return environment


def validate_ignored_import_artifacts(checkout_path: Path) -> None:
    try:
        ignored = subprocess.run(
            [
                "git",
                "-C",
                str(checkout_path),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
            ],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ArtifactError(f"cannot audit ignored evaluator artifacts: {error}") from error
    dangerous = sorted(
        relative.decode("utf-8", errors="surrogateescape")
        for relative in ignored.split(b"\0")
        if relative
        and Path(relative.decode("utf-8", errors="surrogateescape")).suffix.casefold()
        in IMPORT_AFFECTING_IGNORED_SUFFIXES
    )
    if dangerous:
        preview = dangerous[:10]
        suffix = f" (+{len(dangerous) - len(preview)} more)" if len(dangerous) > 10 else ""
        raise ArtifactError(
            "ContextBench checkout has ignored import-affecting Python artifacts: "
            f"{preview}{suffix}"
        )


def validate_runtime_manifest(path: Path, evaluator_path: Path) -> dict[str, Any]:
    manifest = read_json(path, "runtime manifest")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ArtifactError("runtime manifest has an invalid schema")
    environment = manifest.get("environment")
    if not isinstance(environment, dict) or environment.get("SYMBOL_DETAIL_MAX") != "0":
        raise ArtifactError("runtime manifest must pin SYMBOL_DETAIL_MAX=0")
    gold = manifest.get("gold_parquet")
    if not isinstance(gold, dict) or gold.get("sha256") != PINNED_GOLD_SHA256:
        raise ArtifactError("runtime manifest has the wrong gold parquet SHA256")
    gold_path = explicit_file(str(gold.get("path", "")), "runtime gold parquet")
    if sha256_file(gold_path) != PINNED_GOLD_SHA256:
        raise ArtifactError("runtime gold parquet bytes do not match the pinned hash")

    checkout = manifest.get("contextbench_checkout")
    if not isinstance(checkout, dict) or checkout.get("commit") != PINNED_CONTEXTBENCH_COMMIT:
        raise ArtifactError("runtime manifest has the wrong ContextBench commit")
    checkout_path = explicit_path(str(checkout.get("path", "")), "ContextBench checkout")
    try:
        actual_commit = subprocess.run(
            ["git", "-C", str(checkout_path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ArtifactError(f"cannot verify ContextBench checkout commit: {error}") from error
    if actual_commit != PINNED_CONTEXTBENCH_COMMIT:
        raise ArtifactError("ContextBench checkout is not at the pinned commit")
    if checkout.get("clean") is not True:
        raise ArtifactError("runtime manifest must assert a clean ContextBench checkout")
    try:
        dirty = subprocess.run(
            ["git", "-C", str(checkout_path), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ArtifactError(f"cannot verify ContextBench checkout cleanliness: {error}") from error
    if dirty:
        raise ArtifactError("ContextBench checkout has tracked or untracked modifications")
    validate_ignored_import_artifacts(checkout_path)
    evaluator = manifest.get("evaluator")
    if not isinstance(evaluator, dict):
        raise ArtifactError("runtime manifest has no evaluator identity")
    manifest_evaluator = explicit_file(str(evaluator.get("path", "")), "runtime evaluator")
    if manifest_evaluator != evaluator_path:
        raise ArtifactError("runtime evaluator path differs from --evaluator")
    evaluator_hash = sha256_file(evaluator_path)
    if evaluator.get("sha256") != evaluator_hash:
        raise ArtifactError("runtime evaluator SHA256 is stale")
    if checkout_path not in evaluator_path.parents:
        raise ArtifactError("runtime evaluator is not inside the pinned ContextBench checkout")
    if environment.get("PYTHONPATH") != str(checkout_path):
        raise ArtifactError("runtime manifest must import ContextBench from the pinned checkout")

    python = manifest.get("python")
    if not isinstance(python, dict) or python.get("version") != PINNED_PYTHON_VERSION:
        raise ArtifactError("runtime manifest must pin Python 3.11.15")
    python_path = Path(str(python.get("executable", ""))).expanduser()
    if (
        not python_path.is_absolute()
        or any(part.casefold() == "latest" for part in python_path.parts)
        or not python_path.is_file()
    ):
        raise ArtifactError("runtime Python must be an explicit existing executable path")
    script = (
        "import json,sys; from importlib.metadata import version; "
        f"names={list(PINNED_DEPENDENCIES)!r}; "
        "print(json.dumps({'python': '.'.join(map(str,sys.version_info[:3])), "
        "'dependencies': {name: version(name) for name in names}}, sort_keys=True))"
    )
    try:
        runtime = json.loads(
            subprocess.run(
                [str(python_path), "-c", script],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise ArtifactError(f"cannot verify evaluator Python runtime: {error}") from error
    if runtime.get("python") != PINNED_PYTHON_VERSION:
        raise ArtifactError("evaluator Python runtime is not 3.11.15")
    if runtime.get("dependencies") != PINNED_DEPENDENCIES:
        raise ArtifactError("evaluator dependency versions differ from the pinned runtime")
    if manifest.get("dependencies") != PINNED_DEPENDENCIES:
        raise ArtifactError("runtime manifest dependency versions are not pinned exactly")
    import_environment = dict(os.environ)
    import_environment.update(evaluator_execution_environment(manifest))
    import_script = (
        "import hashlib,json,pathlib,contextbench.evaluate as e; "
        "p=pathlib.Path(e.__file__).resolve(); "
        "print(json.dumps({'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))"
    )
    try:
        imported = json.loads(
            subprocess.run(
                [str(python_path), "-c", import_script],
                check=True,
                capture_output=True,
                text=True,
                env=import_environment,
            ).stdout
        )
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise ArtifactError(f"cannot verify imported ContextBench evaluator: {error}") from error
    if imported != {"path": str(evaluator_path), "sha256": evaluator_hash}:
        raise ArtifactError("evaluator runtime imports a different ContextBench source tree")
    expected_template = [
        "{python}",
        "-m",
        "contextbench.evaluate",
        "--gold",
        "{gold}",
        "--pred",
        "{pred}",
        "--cache",
        "{cache}",
        "--out",
        "{out}",
    ]
    if manifest.get("invocation_template") != expected_template:
        raise ArtifactError("runtime manifest has the wrong official evaluator invocation template")
    return manifest


def run_evaluator_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    prediction_path: Path,
    result_path: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise ArtifactError(f"official evaluator could not start: {error}") from error
    write_bytes(stdout_path, completed.stdout)
    write_bytes(stderr_path, completed.stderr)
    if completed.returncode != 0:
        raise ArtifactError(
            f"official evaluator exited {completed.returncode}; see {stderr_path}"
        )
    if not result_path.is_file():
        raise ArtifactError("official evaluator returned success without a result file")
    return {
        "command": command,
        "cwd": str(cwd),
        "environment": {
            "PYTHONPATH": environment["PYTHONPATH"],
            "SYMBOL_DETAIL_MAX": environment["SYMBOL_DETAIL_MAX"],
            "PYTHONDONTWRITEBYTECODE": environment["PYTHONDONTWRITEBYTECODE"],
            "PYTHONNOUSERSITE": environment["PYTHONNOUSERSITE"],
        },
        "returncode": completed.returncode,
        "prediction_sha256": sha256_file(prediction_path),
        "result_sha256": sha256_file(result_path),
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_sha256": sha256_file(stderr_path),
    }


def evaluate_and_seal_batch(
    checkpoint_dir: Path,
    evaluator_path: Path,
    runtime_manifest_path: Path,
    cache_dir: Path,
    candidate_treatment: str | None = None,
) -> dict[str, Any]:
    checkpoint = verify_frozen_checkpoint(checkpoint_dir)
    validate_checkpoint_candidate_treatment(
        checkpoint,
        candidate_treatment,
        require_explicit=True,
    )
    if (checkpoint_dir / "evaluation").exists():
        raise ArtifactError(
            f"refusing to replace sealed batch evaluation: {checkpoint_dir / 'evaluation'}"
        )
    runtime = validate_runtime_manifest(runtime_manifest_path, evaluator_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not cache_dir.is_dir():
        raise ArtifactError(f"evaluator cache is not a directory: {cache_dir}")
    python_path = Path(runtime["python"]["executable"])
    checkout = Path(runtime["contextbench_checkout"]["path"])
    gold = Path(runtime["gold_parquet"]["path"])
    environment = dict(os.environ)
    environment.update(evaluator_execution_environment(runtime))
    with tempfile.TemporaryDirectory(prefix="contextbench-paired-eval-") as directory:
        working = Path(directory)
        worktree_root = working / "worktrees"
        worktree_root.mkdir()
        environment["CONTEXTBENCH_TMP_ROOT"] = str(worktree_root)
        receipts: dict[str, Any] = {
            "mode": "internal_subprocess",
            "worktree_isolation": dict(PAIRED_EVALUATOR_WORKTREE_ISOLATION),
        }
        paths: dict[str, Path] = {}
        for role in ("control", "candidate"):
            prediction_path = checkpoint_dir / f"{role}-batch-predictions.jsonl"
            result_path = working / f"{role}-results.jsonl"
            stdout_path = working / f"{role}.stdout.log"
            stderr_path = working / f"{role}.stderr.log"
            command = [
                str(python_path),
                "-m",
                "contextbench.evaluate",
                "--gold",
                str(gold),
                "--pred",
                str(prediction_path),
                "--cache",
                str(cache_dir),
                "--out",
                str(result_path),
            ]
            receipts[role] = run_evaluator_command(
                command,
                cwd=checkout,
                environment=environment,
                prediction_path=prediction_path,
                result_path=result_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            paths[f"{role}_results"] = result_path
            paths[f"{role}_stdout"] = stdout_path
            paths[f"{role}_stderr"] = stderr_path
        return seal_batch_evaluation(
            checkpoint_dir,
            paths["control_results"],
            paths["candidate_results"],
            evaluator_path,
            runtime_manifest_path,
            paths["control_stdout"],
            paths["control_stderr"],
            paths["candidate_stdout"],
            paths["candidate_stderr"],
            receipts,
            candidate_treatment,
        )


def seal_batch_evaluation(
    checkpoint_dir: Path,
    control_results_path: Path,
    candidate_results_path: Path,
    evaluator_path: Path,
    runtime_manifest_path: Path,
    control_stdout_path: Path,
    control_stderr_path: Path,
    candidate_stdout_path: Path,
    candidate_stderr_path: Path,
    execution_receipt: dict[str, Any],
    candidate_treatment: str | None = None,
) -> dict[str, Any]:
    checkpoint = verify_frozen_checkpoint(checkpoint_dir)
    treatment = validate_checkpoint_candidate_treatment(
        checkpoint,
        candidate_treatment,
        require_explicit=True,
    )
    runtime_manifest = validate_runtime_manifest(runtime_manifest_path, evaluator_path)
    if (
        not isinstance(execution_receipt, dict)
        or execution_receipt.get("mode") != "internal_subprocess"
        or execution_receipt.get("worktree_isolation")
        != PAIRED_EVALUATOR_WORKTREE_ISOLATION
        or execution_receipt.get("control", {}).get("returncode") != 0
        or execution_receipt.get("candidate", {}).get("returncode") != 0
    ):
        raise ArtifactError("batch sealing requires a successful internal evaluator receipt")
    batch_ids = manifest_ids(
        checkpoint_dir / "batch-manifest.json", expected_count=CHECKPOINT_SIZE
    )
    control_results = index_exact_rows(
        read_jsonl(control_results_path, "control batch results"),
        batch_ids,
        "control batch results",
    )
    candidate_results = index_exact_rows(
        read_jsonl(candidate_results_path, "candidate batch results"),
        batch_ids,
        "candidate batch results",
    )
    stored_control_results = index_exact_rows(
        read_jsonl(
            checkpoint_dir / "control-batch-stored-results.jsonl",
            "stored official control results",
        ),
        batch_ids,
        "stored official control results",
    )
    for instance_id in batch_ids:
        if control_results[instance_id] != stored_control_results[instance_id]:
            raise ArtifactError(
                f"local control re-evaluation differs from stored official result: {instance_id}"
            )
    control_predictions = index_exact_rows(
        read_jsonl(checkpoint_dir / "control-batch-predictions.jsonl", "control batch predictions"),
        batch_ids,
        "control batch predictions",
    )
    candidate_predictions = index_exact_rows(
        read_jsonl(
            checkpoint_dir / "candidate-batch-predictions.jsonl",
            "candidate batch predictions",
        ),
        batch_ids,
        "candidate batch predictions",
    )
    for instance_id in batch_ids:
        task_metrics(instance_id, control_predictions[instance_id], control_results[instance_id])
        task_metrics(instance_id, candidate_predictions[instance_id], candidate_results[instance_id])

    receipt_paths = {
        "control": (
            checkpoint_dir / "control-batch-predictions.jsonl",
            control_results_path,
            control_stdout_path,
            control_stderr_path,
        ),
        "candidate": (
            checkpoint_dir / "candidate-batch-predictions.jsonl",
            candidate_results_path,
            candidate_stdout_path,
            candidate_stderr_path,
        ),
    }
    for role, paths in receipt_paths.items():
        receipt = execution_receipt.get(role)
        if not isinstance(receipt, dict):
            raise ArtifactError(f"internal evaluator receipt is missing {role}")
        expected_hashes = {
            "prediction_sha256": sha256_file(paths[0]),
            "result_sha256": sha256_file(paths[1]),
            "stdout_sha256": sha256_file(paths[2]),
            "stderr_sha256": sha256_file(paths[3]),
        }
        if any(receipt.get(key) != value for key, value in expected_hashes.items()):
            raise ArtifactError(f"internal evaluator receipt hashes disagree for {role}")
        if receipt.get("environment") != evaluator_execution_environment(runtime_manifest):
            raise ArtifactError(f"internal evaluator environment disagrees for {role}")

    target = checkpoint_dir / "evaluation"
    if target.exists():
        raise ArtifactError(f"refusing to replace sealed batch evaluation: {target}")
    payload = {
        "control-results.jsonl": control_results_path.read_bytes(),
        "candidate-results.jsonl": candidate_results_path.read_bytes(),
        "control-evaluator.stdout.log": control_stdout_path.read_bytes(),
        "control-evaluator.stderr.log": control_stderr_path.read_bytes(),
        "candidate-evaluator.stdout.log": candidate_stdout_path.read_bytes(),
        "candidate-evaluator.stderr.log": candidate_stderr_path.read_bytes(),
        "runtime-manifest.json": runtime_manifest_path.read_bytes(),
    }
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}
    binding = {
        "checkpoint_sha256": sha256_file(checkpoint_dir / "checkpoint.json"),
        "batch_manifest_sha256": sha256_file(checkpoint_dir / "batch-manifest.json"),
        "control_predictions_sha256": sha256_file(
            checkpoint_dir / "control-batch-predictions.jsonl"
        ),
        "candidate_predictions_sha256": sha256_file(
            checkpoint_dir / "candidate-batch-predictions.jsonl"
        ),
        "stored_control_results_sha256": sha256_file(
            checkpoint_dir / "control-batch-stored-results.jsonl"
        ),
        "gold_sha256": PINNED_GOLD_SHA256,
        "evaluator_sha256": sha256_file(evaluator_path),
        "runtime_manifest_sha256": sha256_file(runtime_manifest_path),
        "invocation_template": runtime_manifest["invocation_template"],
        "environment": runtime_manifest["environment"],
        "execution_receipt_sha256": canonical_sha256(execution_receipt),
        "candidate_treatment": treatment,
        "sealed_output_sha256": hashes,
    }
    seal = {
        "schema_version": SCHEMA_VERSION,
        "batch_ids": batch_ids,
        "batch_manifest_sha256": sha256_file(checkpoint_dir / "batch-manifest.json"),
        "evaluator": {
            "path": str(evaluator_path),
            "sha256": sha256_file(evaluator_path),
            "environment": runtime_manifest["environment"],
            "same_entrypoint_required_for_control_and_candidate": True,
        },
        "runtime_manifest_sha256": sha256_file(runtime_manifest_path),
        "runtime": runtime_manifest,
        "control_re_evaluation_matches_stored_official_results": True,
        "execution_receipt": execution_receipt,
        "candidate_treatment": treatment,
        "evaluation_binding": binding,
        "evaluation_binding_sha256": canonical_sha256(binding),
        "source_outputs": {
            "control_results": str(control_results_path),
            "candidate_results": str(candidate_results_path),
            "control_stdout": str(control_stdout_path),
            "control_stderr": str(control_stderr_path),
            "candidate_stdout": str(candidate_stdout_path),
            "candidate_stderr": str(candidate_stderr_path),
        },
        "sealed_files": hashes,
        "checkpoint_sha256": sha256_file(checkpoint_dir / "checkpoint.json"),
    }
    temporary = Path(tempfile.mkdtemp(prefix=".evaluation-", dir=checkpoint_dir))
    try:
        for name, data in payload.items():
            write_bytes(temporary / name, data)
        write_bytes(temporary / "batch-evaluation.json", json_bytes(seal))
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "seal-batch",
        "checkpoint": str(checkpoint_dir),
        "completed_count": checkpoint.get("completed_count"),
        "batch_ids": batch_ids,
        "evaluation": str(target),
        "evaluator_sha256": seal["evaluator"]["sha256"],
        "candidate_treatment": treatment,
        "source_run_mutated": False,
    }


def load_sealed_batch(checkpoint: dict[str, Any]) -> dict[str, Any]:
    directory = checkpoint["dir"]
    treatment = validate_checkpoint_candidate_treatment(checkpoint["meta"])
    evaluation = directory / "evaluation"
    seal = read_json(evaluation / "batch-evaluation.json", "batch evaluation seal")
    if not isinstance(seal, dict) or seal.get("schema_version") != SCHEMA_VERSION:
        raise ArtifactError(f"invalid batch evaluation seal: {evaluation}")
    if seal.get("batch_ids") != checkpoint["batch_ids"]:
        raise ArtifactError(f"batch evaluation IDs disagree at {directory.name}")
    if seal.get("batch_manifest_sha256") != sha256_file(directory / "batch-manifest.json"):
        raise ArtifactError(f"batch evaluation manifest hash disagrees at {directory.name}")
    if seal.get("checkpoint_sha256") != sha256_file(directory / "checkpoint.json"):
        raise ArtifactError(f"batch evaluation checkpoint hash disagrees at {directory.name}")
    if (
        treatment is not None or "candidate_treatment" in seal
    ) and seal.get("candidate_treatment") != treatment:
        raise ArtifactError(f"batch evaluation candidate treatment disagrees at {directory.name}")
    evaluator = seal.get("evaluator")
    if (
        not isinstance(evaluator, dict)
        or not isinstance(evaluator.get("environment"), dict)
        or evaluator["environment"].get("SYMBOL_DETAIL_MAX") != "0"
    ):
        raise ArtifactError("batch evaluation must record SYMBOL_DETAIL_MAX=0")
    runtime_path = evaluation / "runtime-manifest.json"
    runtime_sha = seal.get("runtime_manifest_sha256")
    if not isinstance(runtime_sha, str) or sha256_file(runtime_path) != runtime_sha:
        raise ArtifactError("sealed runtime manifest hash disagrees")
    runtime = read_json(runtime_path, "sealed runtime manifest")
    if runtime != seal.get("runtime"):
        raise ArtifactError("sealed runtime manifest content disagrees with the seal")
    if (
        not isinstance(runtime, dict)
        or runtime.get("environment", {}).get("SYMBOL_DETAIL_MAX") != "0"
        or runtime.get("environment", {}).get("PYTHONPATH")
        != runtime.get("contextbench_checkout", {}).get("path")
        or runtime.get("gold_parquet", {}).get("sha256") != PINNED_GOLD_SHA256
        or runtime.get("contextbench_checkout", {}).get("commit")
        != PINNED_CONTEXTBENCH_COMMIT
        or runtime.get("contextbench_checkout", {}).get("clean") is not True
        or runtime.get("python", {}).get("version") != PINNED_PYTHON_VERSION
        or runtime.get("dependencies") != PINNED_DEPENDENCIES
    ):
        raise ArtifactError("sealed evaluator runtime is not the pinned official runtime")
    if seal.get("control_re_evaluation_matches_stored_official_results") is not True:
        raise ArtifactError("control re-evaluation parity was not sealed")
    receipt = seal.get("execution_receipt")
    if (
        not isinstance(receipt, dict)
        or receipt.get("mode") != "internal_subprocess"
        or receipt.get("control", {}).get("returncode") != 0
        or receipt.get("candidate", {}).get("returncode") != 0
    ):
        raise ArtifactError("sealed evaluation lacks a verified internal execution receipt")
    sealed_files = seal.get("sealed_files")
    if not isinstance(sealed_files, dict):
        raise ArtifactError("batch evaluation has no sealed file hashes")
    for name, expected_hash in sealed_files.items():
        path = evaluation / name
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise ArtifactError(f"sealed evaluator artifact changed: {path}")
    expected_binding = {
        "checkpoint_sha256": sha256_file(directory / "checkpoint.json"),
        "batch_manifest_sha256": sha256_file(directory / "batch-manifest.json"),
        "control_predictions_sha256": sha256_file(
            directory / "control-batch-predictions.jsonl"
        ),
        "candidate_predictions_sha256": sha256_file(
            directory / "candidate-batch-predictions.jsonl"
        ),
        "stored_control_results_sha256": sha256_file(
            directory / "control-batch-stored-results.jsonl"
        ),
        "gold_sha256": PINNED_GOLD_SHA256,
        "evaluator_sha256": evaluator.get("sha256"),
        "runtime_manifest_sha256": runtime_sha,
        "invocation_template": runtime.get("invocation_template"),
        "environment": runtime.get("environment"),
        "execution_receipt_sha256": canonical_sha256(receipt),
        "sealed_output_sha256": sealed_files,
    }
    sealed_binding = seal.get("evaluation_binding")
    if treatment is not None or (
        isinstance(sealed_binding, dict) and "candidate_treatment" in sealed_binding
    ):
        expected_binding["candidate_treatment"] = treatment
    if (
        seal.get("evaluation_binding") != expected_binding
        or seal.get("evaluation_binding_sha256") != canonical_sha256(expected_binding)
    ):
        raise ArtifactError("evaluation outputs are not bound to the frozen inputs/runtime")
    batch_ids = checkpoint["batch_ids"]
    control_predictions = index_exact_rows(
        read_jsonl(directory / "control-batch-predictions.jsonl", "control batch predictions"),
        batch_ids,
        "control batch predictions",
    )
    candidate_predictions = index_exact_rows(
        read_jsonl(
            directory / "candidate-batch-predictions.jsonl",
            "candidate batch predictions",
        ),
        batch_ids,
        "candidate batch predictions",
    )
    control_results = index_exact_rows(
        read_jsonl(evaluation / "control-results.jsonl", "control batch results"),
        batch_ids,
        "control batch results",
    )
    candidate_results = index_exact_rows(
        read_jsonl(evaluation / "candidate-results.jsonl", "candidate batch results"),
        batch_ids,
        "candidate batch results",
    )
    return {
        "ids": batch_ids,
        "control_predictions": control_predictions,
        "candidate_predictions": candidate_predictions,
        "control_results": control_results,
        "candidate_results": candidate_results,
        "evaluator_sha256": evaluator.get("sha256"),
        "runtime_manifest_sha256": runtime_sha,
    }


def synthetic_run(
    ids: Sequence[str],
    predictions: dict[str, dict[str, Any]],
    results: dict[str, dict[str, Any]],
    languages: dict[str, str],
    records: dict[str, dict[str, Any]],
    watchdog: Path | None,
) -> dict[str, Any]:
    per_task = {
        instance_id: task_metrics(instance_id, predictions[instance_id], results[instance_id])
        for instance_id in ids
    }
    reliability = reliability_from_records(
        ids, predictions, results, records, watchdog if watchdog and watchdog.is_file() else None
    )
    return {
        "ids": list(ids),
        "predictions": predictions,
        "results": results,
        "languages": languages,
        "records": records,
        "per_task": per_task,
        "reliability": reliability,
        "wall_clock": wall_stats(records, None),
    }


def provisional_gate(
    comparison: dict[str, Any],
    languages: dict[str, Any],
    control_reliability: dict[str, Any],
    candidate_reliability: dict[str, Any],
) -> dict[str, Any]:
    global_regressions = sorted(
        granularity
        for granularity in GRANULARITIES
        if comparison["f1_bootstrap"][granularity]["ci_95"]["upper"] < 0.0
    )
    language_regressions = sorted(
        language
        for language, row in languages.items()
        if row["statistically_significant_regression"]
    )
    return {
        "passed": (
            not global_regressions
            and not language_regressions
            and candidate_reliability["failures"] <= control_reliability["failures"]
            and candidate_reliability["timeouts"] <= control_reliability["timeouts"]
            and candidate_reliability["evaluator_errors"]
            <= control_reliability["evaluator_errors"]
        ),
        "provisional": True,
        "release_eligible": False,
        "checks": {
            "no_statistically_confirmed_global_retrieval_f1_regression": not global_regressions,
            "no_statistically_confirmed_language_regression": not language_regressions,
            "failure_count_not_increased": (
                candidate_reliability["failures"] <= control_reliability["failures"]
            ),
            "timeout_count_not_increased": (
                candidate_reliability["timeouts"] <= control_reliability["timeouts"]
            ),
            "evaluator_error_count_not_increased": (
                candidate_reliability["evaluator_errors"]
                <= control_reliability["evaluator_errors"]
            ),
        },
        "failure_reasons": (
            [f"paired_{granularity}_f1_ci_upper_below_zero" for granularity in global_regressions]
            + [f"language_ci_upper_below_zero:{language}" for language in language_regressions]
            + (
                ["failure_count_increased"]
                if candidate_reliability["failures"] > control_reliability["failures"]
                else []
            )
            + (
                ["timeout_count_increased"]
                if candidate_reliability["timeouts"] > control_reliability["timeouts"]
                else []
            )
            + (
                ["evaluator_error_count_increased"]
                if candidate_reliability["evaluator_errors"]
                > control_reliability["evaluator_errors"]
                else []
            )
        ),
        "observed_release_checks_not_enforced_before_n_100": {
            "line_f1_delta_at_least_0_03": comparison["delta"]["line"]["f1"]
            >= MIN_LINE_F1_DELTA - EPSILON,
            "paired_line_f1_ci_lower_above_zero": comparison["line_f1_bootstrap"]["ci_95"][
                "lower"
            ]
            > 0.0,
        },
    }


def compose_checkpoint_gate(
    count: int,
    cumulative_gate: dict[str, Any],
    batch_gate: dict[str, Any],
    full_reconciliation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Make release eligibility inseparable from the complete, passed N=100 gate."""
    passed = bool(cumulative_gate["passed"] and batch_gate["passed"])
    release_eligible = bool(
        count == FULL_MANIFEST_SIZE
        and passed
        and full_reconciliation is not None
    )
    cumulative_gate["release_eligible"] = release_eligible
    return {
        "passed": passed,
        "provisional": count < FULL_MANIFEST_SIZE,
        "release_eligible": release_eligible,
        "checks": {
            "cumulative": cumulative_gate["checks"],
            "latest_batch_10": batch_gate["checks"],
        },
        "failure_reasons": (
            [f"cumulative:{reason}" for reason in cumulative_gate["failure_reasons"]]
            + [f"latest_batch_10:{reason}" for reason in batch_gate["failure_reasons"]]
        ),
        "cumulative_gate": cumulative_gate,
        "latest_batch_gate": batch_gate,
    }


def checkpoint_comparison(
    control_root: Path,
    checkpoint_root: Path,
    count: int,
    candidate_root: Path | None = None,
    candidate_treatment: str | None = None,
) -> dict[str, Any]:
    if count < CHECKPOINT_SIZE or count > FULL_MANIFEST_SIZE or count % CHECKPOINT_SIZE:
        raise ArtifactError("checkpoint count must be a multiple of 10 between 10 and 100")
    if count == FULL_MANIFEST_SIZE and candidate_root is None:
        raise ArtifactError(
            "N=100 requires --candidate-run for full artifact and live terminal reconciliation"
        )
    control = load_run(control_root, expected_count=FULL_MANIFEST_SIZE)
    chain = load_checkpoint_chain(checkpoint_root)
    selected_chain = [checkpoint for checkpoint in chain if len(checkpoint["ids"]) <= count]
    if not selected_chain or len(selected_chain[-1]["ids"]) != count:
        raise ArtifactError(f"checkpoint-{count:04d} is not present in the explicit root")
    if len(selected_chain) != count // CHECKPOINT_SIZE:
        raise ArtifactError("checkpoint chain is incomplete")
    latest = selected_chain[-1]
    source_reconciliation = checkpoint_source_reconciliation(latest["dir"])
    if (latest["dir"] / "manifest.json").read_bytes() != (
        latest["dir"] / "control-manifest.json"
    ).read_bytes():
        raise ArtifactError("cumulative candidate/control checkpoint manifests differ")
    if (latest["dir"] / "source-manifest.json").read_bytes() != (
        control_root / "manifest.json"
    ).read_bytes():
        raise ArtifactError("checkpoint source manifest is not byte-identical to control")
    parity = validate_parity(
        control_root,
        latest["dir"],
        candidate_treatment,
    )

    cumulative_control_predictions: dict[str, dict[str, Any]] = {}
    cumulative_candidate_predictions: dict[str, dict[str, Any]] = {}
    cumulative_control_results: dict[str, dict[str, Any]] = {}
    cumulative_candidate_results: dict[str, dict[str, Any]] = {}
    evaluator_hashes: set[str] = set()
    runtime_manifest_hashes: set[str] = set()
    sealed_batches: list[dict[str, Any]] = []
    for checkpoint in selected_chain:
        validate_checkpoint_candidate_treatment(
            checkpoint["meta"],
            candidate_treatment,
            expected_parity=(
                parity
                if candidate_treatment is not None
                or checkpoint["meta"].get("parity") is not None
                else None
            ),
            require_explicit=True,
        )
        if (checkpoint["dir"] / "batch-manifest.json").read_bytes() != (
            checkpoint["dir"] / "control-batch-manifest.json"
        ).read_bytes():
            raise ArtifactError(f"candidate/control batch manifests differ at {checkpoint['dir'].name}")
        batch = load_sealed_batch(checkpoint)
        evaluator_hash = batch.get("evaluator_sha256")
        if not isinstance(evaluator_hash, str) or not evaluator_hash:
            raise ArtifactError("sealed batch has no evaluator hash")
        evaluator_hashes.add(evaluator_hash)
        runtime_manifest_hash = batch.get("runtime_manifest_sha256")
        if not isinstance(runtime_manifest_hash, str) or not runtime_manifest_hash:
            raise ArtifactError("sealed batch has no runtime manifest hash")
        runtime_manifest_hashes.add(runtime_manifest_hash)
        for instance_id in batch["ids"]:
            if instance_id in cumulative_control_results:
                raise ArtifactError(f"duplicate evaluated checkpoint ID: {instance_id}")
            if batch["control_predictions"][instance_id] != control["predictions"][instance_id]:
                raise ArtifactError(f"frozen control prediction drifted: {instance_id}")
            cumulative_control_predictions[instance_id] = batch["control_predictions"][instance_id]
            cumulative_candidate_predictions[instance_id] = batch["candidate_predictions"][instance_id]
            cumulative_control_results[instance_id] = batch["control_results"][instance_id]
            cumulative_candidate_results[instance_id] = batch["candidate_results"][instance_id]
        sealed_batches.append(
            {
                "count": len(checkpoint["ids"]),
                "ids": batch["ids"],
                "evaluator_sha256": evaluator_hash,
                "runtime_manifest_sha256": runtime_manifest_hash,
                "candidate_treatment": candidate_treatment,
            }
        )
    if len(evaluator_hashes) != 1:
        raise ArtifactError("official evaluator hash changed across checkpoint batches")
    if len(runtime_manifest_hashes) != 1:
        raise ArtifactError("official evaluator runtime changed across checkpoint batches")
    ids = latest["ids"]
    if list(cumulative_control_results) != ids or list(cumulative_candidate_results) != ids:
        raise ArtifactError("accumulated batch results have gaps or differ from cumulative manifest order")

    frozen_candidate_cumulative = index_exact_rows(
        read_jsonl(latest["dir"] / "candidate-predictions.jsonl", "candidate cumulative predictions"),
        ids,
        "candidate cumulative predictions",
    )
    frozen_control_cumulative = index_exact_rows(
        read_jsonl(latest["dir"] / "control-predictions.jsonl", "control cumulative predictions"),
        ids,
        "control cumulative predictions",
    )
    for instance_id in ids:
        if frozen_candidate_cumulative[instance_id] != cumulative_candidate_predictions[instance_id]:
            raise ArtifactError(f"candidate batch accumulation drifted: {instance_id}")
        if frozen_control_cumulative[instance_id] != cumulative_control_predictions[instance_id]:
            raise ArtifactError(f"control batch accumulation drifted: {instance_id}")
    frozen_control_triage = index_exact_rows(
        read_jsonl(latest["dir"] / "control-triage.jsonl", "frozen control triage"),
        ids,
        "frozen control triage",
    )
    for instance_id in ids:
        if frozen_control_triage[instance_id] != control["triage"][instance_id]:
            raise ArtifactError(f"frozen control triage drifted: {instance_id}")

    completion = read_json(latest["dir"] / "completion-records.json")
    candidate_records = completion.get("per_instance") if isinstance(completion, dict) else None
    if not isinstance(candidate_records, dict) or set(candidate_records) != set(ids):
        raise ArtifactError("candidate completion records disagree with cumulative manifest")
    control_records = {instance_id: control["records"][instance_id] for instance_id in ids}
    languages = {instance_id: control["languages"][instance_id] for instance_id in ids}
    control_synthetic = synthetic_run(
        ids,
        cumulative_control_predictions,
        cumulative_control_results,
        languages,
        control_records,
        control_root / "watchdog.log",
    )
    candidate_synthetic = synthetic_run(
        ids,
        cumulative_candidate_predictions,
        cumulative_candidate_results,
        languages,
        candidate_records,
        latest["dir"] / "watchdog.log",
    )
    cumulative = compare_metric_set(ids, control_synthetic, candidate_synthetic)
    cumulative_languages = per_language_comparison(
        ids, control_synthetic, candidate_synthetic
    )

    batch_ids = latest["batch_ids"]
    batch_control = synthetic_run(
        batch_ids,
        {instance_id: cumulative_control_predictions[instance_id] for instance_id in batch_ids},
        {instance_id: cumulative_control_results[instance_id] for instance_id in batch_ids},
        {instance_id: languages[instance_id] for instance_id in batch_ids},
        {instance_id: control_records[instance_id] for instance_id in batch_ids},
        control_root / "watchdog.log",
    )
    batch_candidate = synthetic_run(
        batch_ids,
        {instance_id: cumulative_candidate_predictions[instance_id] for instance_id in batch_ids},
        {instance_id: cumulative_candidate_results[instance_id] for instance_id in batch_ids},
        {instance_id: languages[instance_id] for instance_id in batch_ids},
        {instance_id: candidate_records[instance_id] for instance_id in batch_ids},
        latest["dir"] / "watchdog.log",
    )
    batch = compare_metric_set(batch_ids, batch_control, batch_candidate)
    batch_languages = per_language_comparison(batch_ids, batch_control, batch_candidate)
    batch_gate = provisional_gate(
        batch,
        batch_languages,
        batch_control["reliability"],
        batch_candidate["reliability"],
    )

    full_reconciliation: dict[str, Any] | None = None
    if count == FULL_MANIFEST_SIZE:
        assert candidate_root is not None
        if (candidate_root / "manifest.json").read_bytes() != (
            control_root / "manifest.json"
        ).read_bytes():
            raise ArtifactError("N=100 candidate manifest is not byte-identical to control")
        validate_parity(
            control_root,
            candidate_root,
            candidate_treatment,
        )
        candidate_full = load_run(candidate_root, expected_count=FULL_MANIFEST_SIZE)
        live_terminals = terminal_records(candidate_root, control["ids"])
        if set(live_terminals) != set(ids):
            raise ArtifactError("N=100 candidate does not have exactly 100 terminal run records")
        source_order = {instance_id: index for index, instance_id in enumerate(control["ids"])}
        live_order = sorted(
            ids,
            key=lambda instance_id: (
                live_terminals[instance_id]["run_record_mtime_ns"],
                source_order[instance_id],
            ),
        )
        if live_order != ids:
            raise ArtifactError("N=100 live terminal ordering differs from the frozen chain")
        for instance_id in ids:
            if candidate_full["predictions"][instance_id] != cumulative_candidate_predictions[
                instance_id
            ]:
                raise ArtifactError(f"N=100 frozen prediction differs from full run: {instance_id}")
            if candidate_full["results"][instance_id] != cumulative_candidate_results[instance_id]:
                raise ArtifactError(f"N=100 paired result differs from full official result: {instance_id}")
            frozen = candidate_records[instance_id]
            live = live_terminals[instance_id]
            for key in (
                "status",
                "run_record_mtime_ns",
                "run_record_sha256",
                "prediction_sha256",
                "audit_sha256",
                "query_plan_sha256",
                "failure_sha256",
            ):
                if frozen.get(key) != live.get(key):
                    raise ArtifactError(
                        f"N=100 live terminal artifact differs from frozen chain: {instance_id} {key}"
                    )
        if candidate_full["reliability"] != candidate_synthetic["reliability"]:
            raise ArtifactError("N=100 full-run reliability differs from paired checkpoint accounting")
        full_reconciliation = {
            "candidate_run": str(candidate_root),
            "manifest_byte_identical": True,
            "full_report_publishable": True,
            "full_triage_agreement": True,
            "predictions_identical": True,
            "official_results_identical": True,
            "terminal_artifacts_identical": True,
            "reliability_identical": True,
            "source_snapshot_matches_frozen_chain": True,
            "historical_source_drift_events": len(source_reconciliation["events"]),
        }
        cumulative_gate = release_gate(
            cumulative,
            cumulative_languages,
            control_synthetic["reliability"],
            candidate_synthetic["reliability"],
        )
        cumulative_gate["provisional"] = False
    else:
        cumulative_gate = provisional_gate(
            cumulative,
            cumulative_languages,
            control_synthetic["reliability"],
            candidate_synthetic["reliability"],
        )
    gate = compose_checkpoint_gate(
        count,
        cumulative_gate,
        batch_gate,
        full_reconciliation,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "checkpoint",
        "inputs": {
            "control": str(control_root),
            "checkpoint_root": str(checkpoint_root),
            "count": count,
            "candidate_run": str(candidate_root) if candidate_root is not None else None,
            "candidate_treatment": candidate_treatment,
        },
        "validation": {
            "exact_cumulative_ids": True,
            "exact_batch_ids": True,
            "candidate_control_manifests_identical": True,
            "immutable_checkpoint_chain": True,
            "unique_gap_free_batch_accumulation": True,
            "same_official_evaluator_sha256": next(iter(evaluator_hashes)),
            "same_official_runtime_manifest_sha256": next(iter(runtime_manifest_hashes)),
            "official_evaluator_environment": {"SYMBOL_DETAIL_MAX": "0"},
            "official_evaluations_total_at_n_100": 200,
            "pass_at_1": None,
            "triage_reference_agreement": True,
            "candidate_full_triage_validation": (
                True if count == FULL_MANIFEST_SIZE else "required at N=100"
            ),
            "n_100_full_reconciliation": full_reconciliation,
            "source_reconciliation": source_reconciliation,
            "legacy_failure_attestation_ids": latest["meta"].get(
                "legacy_failure_attestation_ids", []
            ),
            "parity": parity,
        },
        "sealed_batches": sealed_batches,
        "batch": batch,
        "cumulative": cumulative,
        "per_language": {
            "batch": batch_languages,
            "cumulative": cumulative_languages,
        },
        "reliability": {
            "batch": {
                "control": batch_control["reliability"],
                "candidate": batch_candidate["reliability"],
            },
            "cumulative": {
                "control": control_synthetic["reliability"],
                "candidate": candidate_synthetic["reliability"],
            },
        },
        "wall_clock": {
            "batch": {
                "control": batch_control["wall_clock"],
                "candidate": batch_candidate["wall_clock"],
            },
            "cumulative": {
                "control": control_synthetic["wall_clock"],
                "candidate": candidate_synthetic["wall_clock"],
            },
        },
        "gate": gate,
        "per_task": per_task_output(ids, control_synthetic, candidate_synthetic),
    }


def remediation_checkpoint_binding(checkpoint: dict[str, Any]) -> dict[str, Any]:
    directory = checkpoint["dir"]
    evaluation = directory / "evaluation"
    batch = load_sealed_batch(checkpoint)
    return {
        "count": len(checkpoint["ids"]),
        "checkpoint_sha256": sha256_file(directory / "checkpoint.json"),
        "batch_manifest_sha256": sha256_file(directory / "batch-manifest.json"),
        "completion_records_sha256": sha256_file(
            directory / "completion-records.json"
        ),
        "candidate_predictions_sha256": sha256_file(
            directory / "candidate-predictions.jsonl"
        ),
        "batch_evaluation_sha256": sha256_file(
            evaluation / "batch-evaluation.json"
        ),
        "candidate_results_sha256": sha256_file(
            evaluation / "candidate-results.jsonl"
        ),
        "runtime_manifest_sha256": sha256_file(
            evaluation / "runtime-manifest.json"
        ),
        "evaluator_sha256": batch["evaluator_sha256"],
    }


def remediation_checkpoint_binding_v2(checkpoint: dict[str, Any]) -> dict[str, Any]:
    directory = checkpoint["dir"]
    evaluation = directory / "evaluation"
    binding = remediation_checkpoint_binding(checkpoint)
    binding.update(
        {
            "manifest_sha256": sha256_file(directory / "manifest.json"),
            "run_meta_sha256": sha256_file(directory / "run_meta.json"),
            "run_provenance_sha256": sha256_file(
                directory / "run_provenance.json"
            ),
            "control_batch_predictions_sha256": sha256_file(
                directory / "control-batch-predictions.jsonl"
            ),
            "candidate_batch_predictions_sha256": sha256_file(
                directory / "candidate-batch-predictions.jsonl"
            ),
            "control_results_sha256": sha256_file(
                evaluation / "control-results.jsonl"
            ),
            "watchdog_sha256": sha256_file(directory / "watchdog.log"),
            "triage_sha256": sha256_file(directory / "control-triage.jsonl"),
        }
    )
    return binding


def originating_failure_checkpoint(
    chain: Sequence[dict[str, Any]], target_id: str
) -> dict[str, Any]:
    matches = [checkpoint for checkpoint in chain if target_id in checkpoint["batch_ids"]]
    if len(matches) != 1:
        raise ArtifactError(
            "predeclared remediation target must occur in exactly one frozen batch"
        )
    return matches[0]


def false_kill_attestation(
    checkpoint: dict[str, Any], target_id: str
) -> dict[str, Any]:
    directory = checkpoint["dir"]
    completion = read_json(directory / "completion-records.json")
    records = completion.get("per_instance") if isinstance(completion, dict) else None
    if not isinstance(records, dict) or target_id not in records:
        raise ArtifactError("raw remediation target is absent from completion records")
    record = records[target_id]
    if (
        not isinstance(record, dict)
        or record.get("status") != "failure"
        or record.get("validation_mode")
        != "legacy_failure_semantic_attestation_v1"
        or not isinstance(record.get("legacy_failure_attestation"), dict)
    ):
        raise ArtifactError(
            "raw remediation target is not an attested frozen failure"
        )
    legacy = record["legacy_failure_attestation"]
    if (
        legacy.get("exact_zero_prediction_stub_verified") is not True
        or legacy.get("audit_exactly_matches_failure") is not True
        or legacy.get("query_plan_absent") is not True
        or legacy.get("frozen_audit_sha256") != record.get("audit_sha256")
        or legacy.get("frozen_failure_sha256") != record.get("failure_sha256")
        or legacy.get("failure_returncode") != -9
    ):
        raise ArtifactError("raw failure semantic attestation is incomplete")

    failure_relative = record.get("failure_relative_path")
    run_record_relative = record.get("run_record_relative_path")
    if not isinstance(failure_relative, str) or not isinstance(run_record_relative, str):
        raise ArtifactError("raw attested failure has no frozen source artifact paths")
    failure_path = directory / "source-artifacts" / failure_relative
    run_record_path = directory / "source-artifacts" / run_record_relative
    if (
        not failure_path.is_file()
        or sha256_file(failure_path) != record.get("failure_sha256")
        or not run_record_path.is_file()
        or sha256_file(run_record_path) != record.get("run_record_sha256")
    ):
        raise ArtifactError("raw attested failure source artifacts are not hash-bound")
    failure = read_json(failure_path, "frozen false-kill failure")
    runner_seconds = failure.get("seconds") if isinstance(failure, dict) else None
    if (
        not isinstance(failure, dict)
        or failure.get("instance_id") != target_id
        or failure.get("returncode") != -9
        or isinstance(runner_seconds, bool)
        or not isinstance(runner_seconds, (int, float))
        or not math.isfinite(float(runner_seconds))
        or not 0.0 <= float(runner_seconds) < FALSE_KILL_MAX_RECORDED_RUNNER_SECONDS
    ):
        raise ArtifactError("raw failure does not match the proven false-kill shape")

    watchdog_lines = []
    for line in (directory / "watchdog.log").read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
        match = re.search(r"(?:slug|instance_id)=([^\s]+)", line)
        if match and match.group(1) == target_id and "outcome=timeout" in line:
            watchdog_lines.append(line)
    if len(watchdog_lines) != 1:
        raise ArtifactError(
            "proven false-kill remediation requires one exact watchdog timeout line"
        )
    watchdog_line = watchdog_lines[0]
    elapsed_match = re.search(r"\belapsed_s=([0-9]+(?:\.[0-9]+)?)\b", watchdog_line)
    limit_match = re.search(r"\blimit_s=([0-9]+(?:\.[0-9]+)?)\b", watchdog_line)
    if not elapsed_match or not limit_match:
        raise ArtifactError("false-kill watchdog line lacks elapsed_s/limit_s evidence")
    elapsed_seconds = float(elapsed_match.group(1))
    limit_seconds = float(limit_match.group(1))
    if (
        not math.isfinite(elapsed_seconds)
        or not math.isfinite(limit_seconds)
        or elapsed_seconds < FALSE_KILL_MIN_IMPOSSIBLE_ELAPSED_SECONDS
        or limit_seconds <= 0.0
        or elapsed_seconds <= limit_seconds
        or float(runner_seconds) >= limit_seconds
    ):
        raise ArtifactError("watchdog evidence does not prove an impossible elapsed time")
    return {
        "mode": "impossible_watchdog_elapsed_false_kill_v1",
        "target_id": target_id,
        "originating_checkpoint_count": len(checkpoint["ids"]),
        "originating_checkpoint_sha256": sha256_file(
            directory / "checkpoint.json"
        ),
        "run_record_sha256": record["run_record_sha256"],
        "prediction_sha256": record["prediction_sha256"],
        "audit_sha256": record["audit_sha256"],
        "failure_sha256": record["failure_sha256"],
        "watchdog_line_sha256": hashlib.sha256(
            (watchdog_line + "\n").encode("utf-8")
        ).hexdigest(),
        "watchdog_elapsed_seconds": elapsed_seconds,
        "watchdog_limit_seconds": limit_seconds,
        "recorded_runner_seconds": float(runner_seconds),
        "raw_failure_record": copy.deepcopy(record),
    }


def verify_sealed_false_kill_evidence(
    root: Path,
    declaration: dict[str, Any],
    target_id: str,
    raw_record: dict[str, Any],
    raw_chain: Sequence[dict[str, Any]],
) -> None:
    attestation = declaration.get("false_kill_attestation")
    legacy = raw_record.get("legacy_failure_attestation")
    if (
        not isinstance(attestation, dict)
        or attestation.get("mode")
        != "impossible_watchdog_elapsed_false_kill_v1"
        or attestation.get("target_id") != target_id
        or attestation.get("raw_failure_record") != raw_record
        or raw_record.get("status") != "failure"
        or raw_record.get("validation_mode")
        != "legacy_failure_semantic_attestation_v1"
        or not isinstance(legacy, dict)
        or legacy.get("exact_zero_prediction_stub_verified") is not True
        or legacy.get("audit_exactly_matches_failure") is not True
        or legacy.get("query_plan_absent") is not True
        or legacy.get("failure_returncode") != -9
        or legacy.get("frozen_audit_sha256") != raw_record.get("audit_sha256")
        or legacy.get("frozen_failure_sha256") != raw_record.get("failure_sha256")
    ):
        raise ArtifactError("sealed declaration does not bind the raw false-kill record")

    origin_count = attestation.get("originating_checkpoint_count")
    origin = next(
        (row for row in raw_chain if row.get("count") == origin_count), None
    )
    if (
        not isinstance(origin_count, int)
        or not isinstance(origin, dict)
        or origin.get("checkpoint_sha256")
        != attestation.get("originating_checkpoint_sha256")
    ):
        raise ArtifactError("sealed false-kill origin is not raw-chain bound")

    evidence_root = root / "raw-target"
    relative_hashes = {
        "run_record_relative_path": "run_record_sha256",
        "prediction_relative_path": "prediction_sha256",
        "audit_relative_path": "audit_sha256",
        "failure_relative_path": "failure_sha256",
    }
    evidence_paths: dict[str, Path] = {}
    expected_files = {"watchdog.log"}
    for relative_key, digest_key in relative_hashes.items():
        relative_value = raw_record.get(relative_key)
        relative = Path(relative_value) if isinstance(relative_value, str) else None
        if (
            relative is None
            or relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
        ):
            raise ArtifactError("sealed false-kill evidence path is invalid")
        normalized = relative.as_posix()
        path = evidence_root / relative
        if (
            path.is_symlink()
            or not path.is_file()
            or sha256_file(path) != raw_record.get(digest_key)
            or attestation.get(digest_key) != raw_record.get(digest_key)
        ):
            raise ArtifactError("sealed false-kill evidence hash is invalid")
        expected_files.add(normalized)
        evidence_paths[relative_key] = path
    actual_files = {
        path.relative_to(evidence_root).as_posix()
        for path in evidence_root.rglob("*")
        if path.is_file()
    }
    if actual_files != expected_files:
        raise ArtifactError("sealed false-kill evidence inventory is not exhaustive")

    run_record = read_json(
        evidence_paths["run_record_relative_path"], "sealed raw run record"
    )
    failure = read_json(
        evidence_paths["failure_relative_path"], "sealed raw failure"
    )
    audit = read_json(
        evidence_paths["audit_relative_path"], "sealed raw prediction audit"
    )
    prediction_rows = read_jsonl_exact(
        evidence_paths["prediction_relative_path"], "sealed raw prediction"
    )
    prediction = prediction_rows[0] if len(prediction_rows) == 1 else None
    runner_seconds = failure.get("seconds") if isinstance(failure, dict) else None
    trajectory = prediction.get("traj_data") if isinstance(prediction, dict) else None
    if (
        not isinstance(run_record, dict)
        or run_record.get("instance_id") != target_id
        or run_record.get("status") != raw_record.get("status")
        or run_record.get("failure_kind") != raw_record.get("failure_kind")
        or run_record.get("failure_kind") != "nonzero_exit"
        or not isinstance(run_record.get("run_fingerprint"), str)
        or not run_record["run_fingerprint"]
        or not isinstance(failure, dict)
        or failure.get("instance_id") != target_id
        or failure.get("returncode") != -9
        or failure.get("run_fingerprint") != run_record.get("run_fingerprint")
        or audit != {"harness_failure": failure}
        or isinstance(runner_seconds, bool)
        or not isinstance(runner_seconds, (int, float))
        or not math.isfinite(float(runner_seconds))
        or not 0.0 <= float(runner_seconds) < FALSE_KILL_MAX_RECORDED_RUNNER_SECONDS
        or not isinstance(prediction, dict)
        or prediction.get("instance_id") != target_id
        or not isinstance(trajectory, dict)
        or trajectory.get("pred_steps") != []
        or trajectory.get("pred_files") != []
        or trajectory.get("pred_spans") not in ({}, [])
        or trajectory.get("pred_symbols") not in ({}, [])
        or prediction.get("model_patch") != ""
        or not isinstance(prediction.get("harness_failure"), dict)
        or prediction["harness_failure"].get("run_fingerprint")
        != run_record.get("run_fingerprint")
    ):
        raise ArtifactError("sealed raw artifacts do not prove the false-kill shape")

    watchdog_path = evidence_root / "watchdog.log"
    watchdog_lines = watchdog_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
    if len(watchdog_lines) != 1:
        raise ArtifactError("sealed false-kill watchdog evidence is not singular")
    watchdog_line = watchdog_lines[0]
    elapsed_match = re.search(r"\belapsed_s=([0-9]+(?:\.[0-9]+)?)\b", watchdog_line)
    limit_match = re.search(r"\blimit_s=([0-9]+(?:\.[0-9]+)?)\b", watchdog_line)
    elapsed_seconds = float(elapsed_match.group(1)) if elapsed_match else math.nan
    limit_seconds = float(limit_match.group(1)) if limit_match else math.nan
    if (
        target_id not in watchdog_line
        or "outcome=timeout" not in watchdog_line
        or hashlib.sha256((watchdog_line + "\n").encode("utf-8")).hexdigest()
        != attestation.get("watchdog_line_sha256")
        or elapsed_seconds != attestation.get("watchdog_elapsed_seconds")
        or limit_seconds != attestation.get("watchdog_limit_seconds")
        or float(runner_seconds) != attestation.get("recorded_runner_seconds")
        or not math.isfinite(elapsed_seconds)
        or not math.isfinite(limit_seconds)
        or elapsed_seconds < FALSE_KILL_MIN_IMPOSSIBLE_ELAPSED_SECONDS
        or limit_seconds <= 0.0
        or elapsed_seconds <= limit_seconds
        or float(runner_seconds) >= limit_seconds
    ):
        raise ArtifactError("sealed watchdog does not prove an impossible elapsed time")


def predeclare_false_kill(
    control_root: Path,
    checkpoint_root: Path,
    target_id: str,
) -> dict[str, Any]:
    if not target_id.strip():
        raise ArtifactError("remediation target ID must be non-empty")
    chain = load_checkpoint_chain(checkpoint_root)
    origin = originating_failure_checkpoint(chain, target_id)
    origin_count = len(origin["ids"])
    raw = checkpoint_comparison(control_root, checkpoint_root, origin_count)
    attestation = false_kill_attestation(origin, target_id)
    prefix = [checkpoint for checkpoint in chain if len(checkpoint["ids"]) <= origin_count]
    core = {
        "schema_version": SCHEMA_VERSION,
        "mode": "predeclared-false-kill-remediation",
        "target_ids": [target_id],
        "raw_inputs": {
            "control": str(control_root),
            "checkpoint_root": str(checkpoint_root),
            "originating_checkpoint_count": origin_count,
        },
        "raw_chain_prefix": [
            remediation_checkpoint_binding(checkpoint) for checkpoint in prefix
        ],
        "raw_checkpoint_comparison_sha256": canonical_sha256(raw),
        "raw_cumulative_comparison": copy.deepcopy(raw["cumulative"]),
        "raw_cumulative_reliability": copy.deepcopy(raw["reliability"]["cumulative"]),
        "false_kill_attestation": attestation,
        "protocol": {
            "declaration_must_precede_rerun": True,
            "rerun_must_wait_for_raw_n_100_driver_completion": True,
            "rerun_manifest_instances": 1,
            "rerun_concurrency": 1,
            "resume_or_second_attempt_allowed": False,
            "raw_checkpoint_mutation_allowed": False,
        },
    }
    return {**core, "declaration_sha256": canonical_sha256(core)}


def verify_false_kill_declaration(
    declaration: dict[str, Any],
    control_root: Path,
    checkpoint_root: Path,
) -> str:
    if not isinstance(declaration, dict):
        raise ArtifactError("false-kill declaration must be a JSON object")
    digest = declaration.get("declaration_sha256")
    core = _drop_keys(declaration, {"declaration_sha256"})
    if not isinstance(digest, str) or digest != canonical_sha256(core):
        raise ArtifactError("false-kill declaration hash is invalid")
    target_ids = declaration.get("target_ids")
    if (
        declaration.get("schema_version") != SCHEMA_VERSION
        or declaration.get("mode") != "predeclared-false-kill-remediation"
        or not isinstance(target_ids, list)
        or len(target_ids) != 1
        or not isinstance(target_ids[0], str)
        or not target_ids[0]
    ):
        raise ArtifactError("declaration must predeclare exactly one target ID")
    target_id = target_ids[0]
    expected = predeclare_false_kill(control_root, checkpoint_root, target_id)
    if declaration != expected:
        raise ArtifactError(
            "false-kill declaration does not match the immutable raw checkpoint prefix"
        )
    return target_id


def canonical_remediation_attempt_paths(
    checkpoint_root: Path,
    declaration: dict[str, Any],
) -> dict[str, Path]:
    checkpoint_root = checkpoint_root.resolve(strict=True)
    raw_checkpoint = (
        checkpoint_root
        / f"checkpoint-{FULL_MANIFEST_SIZE:04d}"
        / "checkpoint.json"
    )
    target_ids = declaration.get("target_ids") if isinstance(declaration, dict) else None
    declaration_sha256 = (
        declaration.get("declaration_sha256")
        if isinstance(declaration, dict)
        else None
    )
    if (
        raw_checkpoint.is_symlink()
        or not raw_checkpoint.is_file()
        or not isinstance(declaration_sha256, str)
        or not isinstance(target_ids, list)
        or len(target_ids) != 1
        or not isinstance(target_ids[0], str)
        or not target_ids[0]
    ):
        raise ArtifactError("cannot derive the canonical remediation attempt identity")
    attempt_identity = canonical_sha256(
        {
            "schema_version": SCHEMA_VERSION,
            "mode": "false-kill-remediation-attempt-identity",
            "declaration_sha256": declaration_sha256,
            "raw_n_100_checkpoint_sha256": sha256_file(raw_checkpoint),
            "target_id": target_ids[0],
        }
    )
    attempt_root = checkpoint_root.parent / (
        f".contextbench-remediation-{attempt_identity}.attempt"
    )
    return {
        "attempt_root": attempt_root,
        "token": attempt_root / "authorization-token.json",
        "manifest": attempt_root / "rerun-manifest.json",
        "authorized_provenance": attempt_root / "authorized-run-provenance.json",
        "authorization": attempt_root / "authorization.json",
        "ledger": attempt_root / "attempt-ledger.jsonl",
        "rerun": attempt_root / "rerun",
        "bundle": attempt_root / "bundle",
    }


def remediation_policy_semantics(
    provenance: dict[str, Any],
) -> dict[str, Any]:
    policy = provenance.get("policy")
    driver = provenance.get("driver")
    runner = provenance.get("runner")
    if not all(isinstance(value, dict) for value in (policy, driver, runner)):
        raise ArtifactError("raw remediation provenance lacks policy/driver/runner identity")
    driver_sha256 = driver.get("sha256")
    runner_sha256 = runner.get("sha256")
    if not all(
        isinstance(value, str) and value
        for value in (driver_sha256, runner_sha256)
    ):
        raise ArtifactError("raw remediation driver/runner hash is invalid")
    has_selector = "selector_policy" in policy
    has_post_selector = "post_selector_policy" in policy
    if has_selector and has_post_selector:
        selector_policy = policy.get("selector_policy")
        post_selector_policy = policy.get("post_selector_policy")
        if not all(
            isinstance(value, str) and value
            for value in (selector_policy, post_selector_policy)
        ):
            raise ArtifactError("explicit remediation selector policy is invalid")
        return {
            "mode": "explicit-driver-policy-v1",
            "driver_sha256": driver_sha256,
            "runner_sha256": runner_sha256,
            "selector_policy": selector_policy,
            "post_selector_policy": post_selector_policy,
        }
    if not has_selector and not has_post_selector:
        attestation = LEGACY_REMEDIATION_POLICY_ATTESTATIONS.get(driver_sha256)
        if (
            not isinstance(attestation, dict)
            or attestation.get("runner_sha256") != runner_sha256
        ):
            raise ArtifactError(
                "missing selector policy is not attested for this driver/runner"
            )
        return {
            "mode": "attested-legacy-driver-policy-v1",
            "driver_sha256": driver_sha256,
            "runner_sha256": runner_sha256,
            "selector_policy_surface": attestation["selector_policy_surface"],
            "post_selector_policy": attestation["post_selector_policy"],
        }
    raise ArtifactError(
        "raw remediation provenance has a partial selector-policy surface"
    )


def remediation_raw_provenance_binding(
    provenance_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if provenance_path.is_symlink() or not provenance_path.is_file():
        raise ArtifactError("raw N=100 run provenance is missing or symlinked")
    provenance = read_json(provenance_path, "raw N=100 run provenance")
    if not isinstance(provenance, dict):
        raise ArtifactError("raw N=100 run provenance must be an object")
    fingerprint = provenance.get("fingerprint")
    if (
        not isinstance(fingerprint, str)
        or fingerprint
        != canonical_sha256(_drop_keys(provenance, {"fingerprint"}))
    ):
        raise ArtifactError("raw N=100 provenance fingerprint is invalid")
    binding = {
        "file_sha256": sha256_file(provenance_path),
        "bytes": provenance_path.stat().st_size,
        "fingerprint": fingerprint,
        "policy_semantics": remediation_policy_semantics(provenance),
    }
    return provenance, binding


def _authorization_binding_from_token(token: dict[str, Any]) -> dict[str, Any]:
    return {
        "declaration_sha256": token["declaration"]["declaration_sha256"],
        "raw_n_100_checkpoint_sha256": token["raw_n_100"]["checkpoint_sha256"],
        "raw_n_100_provenance_sha256": token["raw_n_100"][
            "run_provenance_sha256"
        ],
        "raw_policy_semantics": copy.deepcopy(
            token["raw_n_100"]["policy_semantics"]
        ),
        "target_id": token["target_id"],
        "rerun_id": token["rerun"]["run_id"],
        "manifest_sha256": token["rerun"]["manifest_sha256"],
        "nonce": token["nonce"],
    }


def verify_remediation_token_self(token_path: Path) -> dict[str, Any]:
    token = read_json(token_path, "remediation authorization token")
    if not isinstance(token, dict):
        raise ArtifactError("remediation authorization token must be an object")
    token_digest = token.get("token_sha256")
    core = _drop_keys(token, {"token_sha256"})
    if (
        token.get("schema_version") != SCHEMA_VERSION
        or token.get("mode") != REMEDIATION_TOKEN_MODE
        or not isinstance(token_digest, str)
        or token_digest != canonical_sha256(core)
    ):
        raise ArtifactError("remediation authorization token hash/schema is invalid")
    nonce = token.get("nonce")
    if not isinstance(nonce, str) or not REMEDIATION_NONCE_RE.fullmatch(nonce):
        raise ArtifactError("remediation authorization nonce is invalid")
    target_id = token.get("target_id")
    rerun = token.get("rerun")
    raw_n_100 = token.get("raw_n_100")
    if (
        not isinstance(target_id, str)
        or not target_id
        or not isinstance(rerun, dict)
        or not isinstance(rerun.get("run_id"), str)
        or not rerun["run_id"]
        or not isinstance(rerun.get("manifest_sha256"), str)
        or not isinstance(rerun.get("manifest_bytes"), int)
        or not isinstance(raw_n_100, dict)
        or not isinstance(raw_n_100.get("run_provenance_sha256"), str)
        or not isinstance(raw_n_100.get("run_provenance_bytes"), int)
        or raw_n_100["run_provenance_bytes"] <= 0
        or not isinstance(raw_n_100.get("policy_semantics"), dict)
    ):
        raise ArtifactError("remediation token has invalid target/rerun identity")
    return token


def _validate_prepared_remediation_token(
    token_path: Path,
    checkpoint_root: Path,
    declaration_path: Path,
    declaration: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    paths = canonical_remediation_attempt_paths(checkpoint_root, declaration)
    if token_path.resolve(strict=True) != paths["token"]:
        raise ArtifactError("authorization token is not at the canonical one-attempt path")
    token = verify_remediation_token_self(token_path)
    target_id = declaration["target_ids"][0]
    latest = state["latest"]["dir"]
    raw_meta = read_json(latest / "run_meta.json", "raw N=100 run metadata")
    raw_provenance, raw_provenance_binding = remediation_raw_provenance_binding(
        latest / "run_provenance.json"
    )
    if not isinstance(raw_meta, dict) or not isinstance(raw_provenance, dict):
        raise ArtifactError("raw N=100 run identity is malformed")
    origin = originating_failure_checkpoint(state["chain"], target_id)
    expected_paths = {key: str(value) for key, value in paths.items()}
    expected = {
        "schema_version": SCHEMA_VERSION,
        "mode": REMEDIATION_TOKEN_MODE,
        "nonce": token["nonce"],
        "target_id": target_id,
        "declaration": {
            "declaration_sha256": declaration["declaration_sha256"],
            "file_sha256": sha256_file(declaration_path),
        },
        "raw_n_100": {
            "checkpoint_sha256": sha256_file(latest / "checkpoint.json"),
            "candidate_run_id": raw_meta.get("run_id"),
            "candidate_fingerprint": raw_provenance.get("fingerprint"),
            "manifest_sha256": sha256_file(latest / "manifest.json"),
            "run_provenance_sha256": raw_provenance_binding["file_sha256"],
            "run_provenance_bytes": raw_provenance_binding["bytes"],
            "policy_semantics": raw_provenance_binding["policy_semantics"],
        },
        "originating_batch": {
            "count": len(origin["ids"]),
            "checkpoint_sha256": sha256_file(origin["dir"] / "checkpoint.json"),
            "batch_manifest_sha256": sha256_file(
                origin["dir"] / "batch-manifest.json"
            ),
        },
        "rerun": copy.deepcopy(token["rerun"]),
        "canonical_paths": expected_paths,
        "protocol": {
            "post_n_100_only": True,
            "authorization_required_before_execution": True,
            "resume_allowed": False,
            "attempts_allowed": 1,
            "bundle_destination_is_canonical": True,
        },
    }
    expected["token_sha256"] = canonical_sha256(expected)
    if token != expected:
        raise ArtifactError(
            "remediation token does not match the current raw N=100/declaration state"
        )
    manifest_path = paths["manifest"]
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_ids(manifest_path, expected_count=1) != [target_id]
        or sha256_file(manifest_path) != token["rerun"]["manifest_sha256"]
        or manifest_path.stat().st_size != token["rerun"]["manifest_bytes"]
    ):
        raise ArtifactError("canonical rerun manifest differs from the authorization token")
    return token


def prepare_remediation_authorization(
    control_root: Path,
    checkpoint_root: Path,
    candidate_root: Path,
    declaration_path: Path,
    rerun_manifest_path: Path,
    rerun_id: str,
    nonce: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", rerun_id or ""):
        raise ArtifactError("remediation rerun ID is empty or path-unsafe")
    if not REMEDIATION_NONCE_RE.fullmatch(nonce or ""):
        raise ArtifactError(
            "remediation nonce must be 32-128 URL-safe alphanumeric characters"
        )
    declaration = read_json(declaration_path, "false-kill declaration")
    target_id = verify_false_kill_declaration(
        declaration, control_root, checkpoint_root
    )
    state = load_n_100_remediation_state(
        control_root, checkpoint_root, candidate_root
    )
    latest = state["latest"]["dir"]
    raw_meta = read_json(latest / "run_meta.json", "raw N=100 run metadata")
    raw_provenance, raw_provenance_binding = remediation_raw_provenance_binding(
        latest / "run_provenance.json"
    )
    if not isinstance(raw_meta, dict) or not isinstance(raw_provenance, dict):
        raise ArtifactError("raw N=100 run identity is malformed")
    if rerun_id == raw_meta.get("run_id"):
        raise ArtifactError("remediation rerun ID must differ from the raw run ID")
    if manifest_ids(rerun_manifest_path, expected_count=1) != [target_id]:
        raise ArtifactError("prepared rerun manifest is not the predeclared target")
    paths = canonical_remediation_attempt_paths(checkpoint_root, declaration)
    attempt_root = paths["attempt_root"]
    if attempt_root.exists() or attempt_root.is_symlink():
        raise ArtifactError(
            f"canonical remediation attempt already exists: {attempt_root}"
        )
    reject_path_overlap(
        attempt_root,
        "canonical remediation attempt",
        (
            ("control source", control_root),
            ("checkpoint source", checkpoint_root),
            ("candidate source", candidate_root),
        ),
    )
    origin = originating_failure_checkpoint(state["chain"], target_id)
    manifest_bytes = rerun_manifest_path.read_bytes()
    token_core = {
        "schema_version": SCHEMA_VERSION,
        "mode": REMEDIATION_TOKEN_MODE,
        "nonce": nonce,
        "target_id": target_id,
        "declaration": {
            "declaration_sha256": declaration["declaration_sha256"],
            "file_sha256": sha256_file(declaration_path),
        },
        "raw_n_100": {
            "checkpoint_sha256": sha256_file(latest / "checkpoint.json"),
            "candidate_run_id": raw_meta.get("run_id"),
            "candidate_fingerprint": raw_provenance.get("fingerprint"),
            "manifest_sha256": sha256_file(latest / "manifest.json"),
            "run_provenance_sha256": raw_provenance_binding["file_sha256"],
            "run_provenance_bytes": raw_provenance_binding["bytes"],
            "policy_semantics": raw_provenance_binding["policy_semantics"],
        },
        "originating_batch": {
            "count": len(origin["ids"]),
            "checkpoint_sha256": sha256_file(origin["dir"] / "checkpoint.json"),
            "batch_manifest_sha256": sha256_file(
                origin["dir"] / "batch-manifest.json"
            ),
        },
        "rerun": {
            "run_id": rerun_id,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "manifest_bytes": len(manifest_bytes),
        },
        "canonical_paths": {key: str(value) for key, value in paths.items()},
        "protocol": {
            "post_n_100_only": True,
            "authorization_required_before_execution": True,
            "resume_allowed": False,
            "attempts_allowed": 1,
            "bundle_destination_is_canonical": True,
        },
    }
    token = {**token_core, "token_sha256": canonical_sha256(token_core)}
    attempt_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{attempt_root.name}-", dir=attempt_root.parent)
    )
    moved = False
    try:
        write_bytes(temporary / "rerun-manifest.json", manifest_bytes)
        write_bytes(temporary / "authorization-token.json", json_bytes(token))
        os.replace(temporary, attempt_root)
        moved = True
    except BaseException:
        if not moved:
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "prepared-remediation-authorization",
        "target_id": target_id,
        "attempt_root": str(attempt_root),
        "authorization_token": str(paths["token"]),
        "rerun_manifest": str(paths["manifest"]),
        "canonical_rerun_root": str(paths["rerun"]),
        "canonical_bundle_root": str(paths["bundle"]),
        "token_file_sha256": sha256_file(paths["token"]),
        "execution_authorized": False,
    }


def snapshot_authorized_rerun_provenance(
    driver_path: Path,
    token_path: Path,
    raw_provenance_path: Path,
    dataset_path: Path,
    manifest_path: Path,
    line_budget: int,
    selector_model: str | None,
    selector_mode: str,
    rerank_model_dir: Path | None,
    reinclude_tracked_dirs: bool,
    query_plans: bool,
    timeout: int,
    chdir: Path,
) -> dict[str, Any]:
    token = verify_remediation_token_self(token_path)
    raw_provenance, raw_binding = remediation_raw_provenance_binding(
        raw_provenance_path
    )
    token_raw = token["raw_n_100"]
    if (
        raw_binding["fingerprint"] != token_raw.get("candidate_fingerprint")
        or raw_binding["file_sha256"]
        != token_raw.get("run_provenance_sha256")
        or raw_binding["bytes"] != token_raw.get("run_provenance_bytes")
        or raw_binding["policy_semantics"]
        != token_raw.get("policy_semantics")
    ):
        raise ArtifactError("raw N=100 provenance differs from the prepared token")
    if manifest_ids(manifest_path, expected_count=1) != [token["target_id"]]:
        raise ArtifactError("provenance snapshot manifest differs from token target")
    if (
        sha256_file(manifest_path) != token["rerun"]["manifest_sha256"]
        or manifest_path.stat().st_size != token["rerun"]["manifest_bytes"]
    ):
        raise ArtifactError("provenance snapshot manifest differs from token bytes")
    raw_driver = raw_provenance["driver"]
    if (
        driver_path.is_symlink()
        or not driver_path.is_file()
        or str(driver_path.resolve(strict=True)) != raw_driver.get("path")
        or driver_path.stat().st_size != raw_driver.get("bytes")
        or sha256_file(driver_path) != raw_driver.get("sha256")
    ):
        raise ArtifactError(
            "snapshot must use the exact raw N=100 parallel driver"
        )
    specification = importlib.util.spec_from_file_location(
        "contextbench_parallel_driver_remediation", driver_path
    )
    if specification is None or specification.loader is None:
        raise ArtifactError("could not load the pinned parallel driver")
    module = importlib.util.module_from_spec(specification)
    try:
        specification.loader.exec_module(module)
    except Exception as error:
        raise ArtifactError(f"could not import pinned parallel driver: {error}") from error
    if not hasattr(module, "build_run_provenance"):
        raise ArtifactError("parallel driver has no build_run_provenance function")
    runner_path = getattr(module, "RUNNER", None)
    raw_runner = raw_provenance["runner"]
    if (
        not isinstance(runner_path, Path)
        or runner_path.is_symlink()
        or not runner_path.is_file()
        or str(runner_path.resolve(strict=True)) != raw_runner.get("path")
        or runner_path.stat().st_size != raw_runner.get("bytes")
        or sha256_file(runner_path) != raw_runner.get("sha256")
    ):
        raise ArtifactError(
            "snapshot must use the exact raw N=100 runner"
        )
    raw_policy = raw_provenance["policy"]
    semantics = raw_binding["policy_semantics"]
    selector_policy = raw_policy.get("selector_policy")
    post_selector_policy = semantics["post_selector_policy"]
    namespace = argparse.Namespace(
        dataset=dataset_path,
        manifest=manifest_path,
        concurrency=1,
        line_budget=line_budget,
        selector_model=selector_model,
        selector_mode=selector_mode,
        selector_policy=selector_policy,
        post_selector_policy=post_selector_policy,
        rerank_model_dir=rerank_model_dir,
        reinclude_tracked_dirs=reinclude_tracked_dirs,
        query_plans=query_plans,
        timeout=timeout,
        provenance_file=token_path,
        chdir=chdir,
    )
    provenance = module.build_run_provenance(namespace)
    if (
        not isinstance(provenance, dict)
        or provenance.get("fingerprint")
        != canonical_sha256(_drop_keys(provenance, {"fingerprint"}))
    ):
        raise ArtifactError("parallel driver produced invalid prospective provenance")
    if _normalized_remediation_provenance(
        raw_provenance
    ) != _normalized_remediation_provenance(provenance):
        raise ArtifactError(
            "prospective provenance changes raw driver/runner/policy/runtime identity"
        )
    return provenance


def authorize_remediation_execution(
    control_root: Path,
    checkpoint_root: Path,
    candidate_root: Path,
    declaration_path: Path,
    token_path: Path,
    prospective_provenance_path: Path,
) -> dict[str, Any]:
    declaration = read_json(declaration_path, "false-kill declaration")
    target_id = verify_false_kill_declaration(
        declaration, control_root, checkpoint_root
    )
    state = load_n_100_remediation_state(
        control_root, checkpoint_root, candidate_root
    )
    token = _validate_prepared_remediation_token(
        token_path, checkpoint_root, declaration_path, declaration, state
    )
    paths = canonical_remediation_attempt_paths(checkpoint_root, declaration)
    if any(paths[name].exists() or paths[name].is_symlink() for name in (
        "authorization", "ledger", "authorized_provenance", "rerun", "bundle"
    )):
        raise ArtifactError("remediation attempt is already authorized or has execution residue")
    provenance = read_json(
        prospective_provenance_path, "prospective remediation provenance"
    )
    if not isinstance(provenance, dict):
        raise ArtifactError("prospective remediation provenance must be an object")
    fingerprint = provenance.get("fingerprint")
    raw_provenance, raw_provenance_binding = remediation_raw_provenance_binding(
        state["latest"]["dir"] / "run_provenance.json"
    )
    if (
        raw_provenance_binding["file_sha256"]
        != token["raw_n_100"]["run_provenance_sha256"]
        or raw_provenance_binding["bytes"]
        != token["raw_n_100"]["run_provenance_bytes"]
        or raw_provenance_binding["policy_semantics"]
        != token["raw_n_100"]["policy_semantics"]
        or not isinstance(fingerprint, str)
        or not fingerprint
        or fingerprint == raw_provenance.get("fingerprint")
        or fingerprint
        != canonical_sha256(_drop_keys(provenance, {"fingerprint"}))
    ):
        raise ArtifactError("authorized rerun fingerprint is missing or not distinct")
    manifest_identity = provenance.get("manifest")
    memtrace_identity = provenance.get("memtrace")
    explicit_identity = (
        memtrace_identity.get("explicit_provenance")
        if isinstance(memtrace_identity, dict)
        else None
    )
    if (
        not isinstance(manifest_identity, dict)
        or manifest_identity.get("sha256") != token["rerun"]["manifest_sha256"]
        or manifest_identity.get("bytes") != token["rerun"]["manifest_bytes"]
        or not isinstance(explicit_identity, dict)
        or explicit_identity.get("exists") is not True
        or explicit_identity.get("kind") != "file"
        or explicit_identity.get("sha256") != sha256_file(token_path)
        or explicit_identity.get("bytes") != token_path.stat().st_size
    ):
        raise ArtifactError("prospective provenance is not bound to token/manifest")
    policy = provenance.get("policy")
    behavior = provenance.get("behavior_environment")
    if (
        not isinstance(policy, dict)
        or policy.get("concurrency") != 1
        or not isinstance(behavior, dict)
        or behavior.get("MEMTRACE_PIN_SLOTS") != "1"
    ):
        raise ArtifactError("prospective remediation provenance is not single-slot")
    if _normalized_remediation_provenance(raw_provenance) != (
        _normalized_remediation_provenance(provenance)
    ):
        raise ArtifactError("prospective remediation provenance changes raw policy/runtime")

    provenance_bytes = prospective_provenance_path.read_bytes()
    provenance_file_sha256 = hashlib.sha256(provenance_bytes).hexdigest()
    authorization_core = {
        "schema_version": SCHEMA_VERSION,
        "mode": REMEDIATION_AUTHORIZATION_MODE,
        "binding": _authorization_binding_from_token(token),
        "token_sha256": token["token_sha256"],
        "token_file_sha256": sha256_file(token_path),
        "authorized_provenance_sha256": provenance_file_sha256,
        "rerun_fingerprint": fingerprint,
        "canonical_paths": copy.deepcopy(token["canonical_paths"]),
        "protocol": {
            "execution_may_begin_after_this_authorization": True,
            "resume_allowed": False,
            "attempts_allowed": 1,
            "append_only_ledger_required": True,
        },
    }
    authorization = {
        **authorization_core,
        "authorization_sha256": canonical_sha256(authorization_core),
    }
    authorization_bytes = json_bytes(authorization)
    authorization_file_sha256 = hashlib.sha256(authorization_bytes).hexdigest()
    ledger_core = {
        "schema_version": SCHEMA_VERSION,
        "mode": REMEDIATION_LEDGER_MODE,
        "sequence": 1,
        "event": "execution_authorized",
        "authorization_sha256": authorization["authorization_sha256"],
        "authorization_file_sha256": authorization_file_sha256,
        "binding": copy.deepcopy(authorization["binding"]),
        "rerun_fingerprint": fingerprint,
    }
    ledger_entry = {
        **ledger_core,
        "entry_sha256": canonical_sha256(ledger_core),
    }
    write_exclusive_group(
        (
            (paths["authorized_provenance"], provenance_bytes),
            (paths["authorization"], authorization_bytes),
            (paths["ledger"], jsonl_bytes([ledger_entry])),
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "authorized-remediation-execution",
        "target_id": target_id,
        "authorization": str(paths["authorization"]),
        "authorization_sha256": authorization["authorization_sha256"],
        "ledger": str(paths["ledger"]),
        "canonical_rerun_root": str(paths["rerun"]),
        "canonical_bundle_root": str(paths["bundle"]),
        "rerun_id": token["rerun"]["run_id"],
        "rerun_fingerprint": fingerprint,
        "execution_authorized": True,
        "resume_allowed": False,
    }


def _normalized_remediation_run_meta(value: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(value)
    for key in ("run_id", "concurrency", "manifest_instances"):
        normalized.pop(key, None)
    core_pinning = normalized.get("core_pinning")
    if not isinstance(core_pinning, dict):
        raise ArtifactError("remediation run metadata has no core_pinning object")
    core_pinning.pop("slots", None)
    return normalized


def _normalized_remediation_provenance(value: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(value)
    normalized.pop("fingerprint", None)
    normalized.pop("manifest", None)
    policy = normalized.get("policy")
    if not isinstance(policy, dict):
        raise ArtifactError("remediation provenance has no policy object")
    policy.pop("concurrency", None)
    behavior = normalized.get("behavior_environment")
    if not isinstance(behavior, dict):
        raise ArtifactError("remediation provenance has no behavior_environment object")
    behavior.pop("MEMTRACE_PIN_SLOTS", None)
    memtrace = normalized.get("memtrace")
    if not isinstance(memtrace, dict):
        raise ArtifactError("remediation provenance has no memtrace object")
    memtrace.pop("explicit_provenance", None)
    return normalized


def validate_remediation_parity(
    raw_checkpoint_dir: Path,
    rerun_root: Path,
) -> dict[str, Any]:
    raw_meta = read_json(raw_checkpoint_dir / "run_meta.json", "raw run metadata")
    rerun_meta = read_json(rerun_root / "run_meta.json", "rerun metadata")
    raw_provenance = read_json(
        raw_checkpoint_dir / "run_provenance.json", "raw run provenance"
    )
    rerun_provenance = read_json(
        rerun_root / "run_provenance.json", "rerun provenance"
    )
    if any(
        not isinstance(value, dict)
        for value in (raw_meta, rerun_meta, raw_provenance, rerun_provenance)
    ):
        raise ArtifactError("remediation run metadata/provenance must be JSON objects")

    raw_run_id = raw_meta.get("run_id")
    rerun_run_id = rerun_meta.get("run_id")
    if (
        not isinstance(raw_run_id, str)
        or not raw_run_id
        or not isinstance(rerun_run_id, str)
        or not rerun_run_id
        or raw_run_id == rerun_run_id
    ):
        raise ArtifactError("remediation rerun must have one distinct run ID")
    if raw_meta.get("manifest_instances") != FULL_MANIFEST_SIZE:
        raise ArtifactError("raw remediation metadata is not the 100-task run")
    if rerun_meta.get("manifest_instances") != 1:
        raise ArtifactError("remediation rerun metadata must declare one manifest task")
    if rerun_meta.get("concurrency") != 1:
        raise ArtifactError("remediation rerun concurrency must be exactly one")
    raw_concurrency = raw_meta.get("concurrency")
    if (
        isinstance(raw_concurrency, bool)
        or not isinstance(raw_concurrency, int)
        or raw_concurrency < 1
    ):
        raise ArtifactError("raw remediation concurrency is invalid")
    raw_pinning = raw_meta.get("core_pinning")
    rerun_pinning = rerun_meta.get("core_pinning")
    if (
        not isinstance(raw_pinning, dict)
        or not isinstance(rerun_pinning, dict)
        or raw_pinning.get("slots") != raw_concurrency
        or rerun_pinning.get("slots") != 1
    ):
        raise ArtifactError("remediation core-pinning slots do not match concurrency")
    if _normalized_remediation_run_meta(raw_meta) != _normalized_remediation_run_meta(
        rerun_meta
    ):
        raise ArtifactError(
            "remediation run metadata differs outside run_id, concurrency, "
            "manifest_instances, and core_pinning.slots"
        )

    raw_fingerprint = raw_provenance.get("fingerprint")
    rerun_fingerprint = rerun_provenance.get("fingerprint")
    if (
        not isinstance(raw_fingerprint, str)
        or not raw_fingerprint
        or not isinstance(rerun_fingerprint, str)
        or not rerun_fingerprint
        or raw_fingerprint == rerun_fingerprint
    ):
        raise ArtifactError("remediation rerun must have a distinct provenance fingerprint")
    raw_policy = raw_provenance.get("policy")
    rerun_policy = rerun_provenance.get("policy")
    if (
        not isinstance(raw_policy, dict)
        or not isinstance(rerun_policy, dict)
        or rerun_policy.get("concurrency") != 1
    ):
        raise ArtifactError("remediation rerun policy must pin concurrency=1")
    raw_behavior = raw_provenance.get("behavior_environment")
    rerun_behavior = rerun_provenance.get("behavior_environment")
    if (
        not isinstance(raw_behavior, dict)
        or not isinstance(rerun_behavior, dict)
        or raw_behavior.get("MEMTRACE_PIN_SLOTS") != str(raw_concurrency)
        or rerun_behavior.get("MEMTRACE_PIN_SLOTS") != "1"
    ):
        raise ArtifactError("remediation provenance core-pinning slots are invalid")
    if _normalized_remediation_provenance(
        raw_provenance
    ) != _normalized_remediation_provenance(rerun_provenance):
        raise ArtifactError(
            "remediation provenance differs outside fingerprint, one-task manifest, "
            "concurrency, and behavior_environment.MEMTRACE_PIN_SLOTS"
        )

    manifest_bytes = (rerun_root / "manifest.json").read_bytes()
    manifest_identity = rerun_provenance.get("manifest")
    if (
        not isinstance(manifest_identity, dict)
        or manifest_identity.get("bytes") != len(manifest_bytes)
        or manifest_identity.get("sha256")
        != hashlib.sha256(manifest_bytes).hexdigest()
        or not isinstance(manifest_identity.get("path"), str)
        or not manifest_identity["path"]
    ):
        raise ArtifactError("remediation provenance is not bound to its one-task manifest")

    product = raw_meta.get("memtrace_source")
    if (
        not isinstance(product, dict)
        or product != rerun_meta.get("memtrace_source")
        or not isinstance(product.get("head_sha"), str)
        or not product["head_sha"]
        or not isinstance(product.get("binary_sha256"), str)
        or not product["binary_sha256"]
    ):
        raise ArtifactError("remediation product SHA/binary identity is missing or changed")
    return {
        "same_product_head_sha": product["head_sha"],
        "same_product_binary_sha256": product["binary_sha256"],
        "same_model_and_policy": True,
        "same_runner_driver_and_behavior_environment": True,
        "allowed_run_meta_differences": list(
            REMEDIATION_RUN_META_ALLOWED_DIFFERENCES
        ),
        "allowed_provenance_differences": list(
            REMEDIATION_PROVENANCE_ALLOWED_DIFFERENCES
        ),
        "recorded_resource_identity": {
            "concurrency": {"raw": raw_concurrency, "rerun": 1},
            "core_pinning_slots": {"raw": raw_concurrency, "rerun": 1},
            "all_other_recorded_resource_fields_identical": True,
            "scope": (
                "parity is limited to recorded metadata; exact physical-host "
                "equivalence is not asserted"
            ),
        },
        "run_id_difference": {"raw": raw_run_id, "rerun": rerun_run_id},
        "manifest_scope_difference": {
            "raw_instances": FULL_MANIFEST_SIZE,
            "rerun_instances": 1,
        },
    }


def _validate_hashed_object(value: dict[str, Any], digest_key: str, label: str) -> None:
    digest = value.get(digest_key)
    core = _drop_keys(value, {digest_key})
    if not isinstance(digest, str) or digest != canonical_sha256(core):
        raise ArtifactError(f"{label} hash is invalid")


def _read_remediation_ledger(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ArtifactError("remediation attempt ledger is missing or symlinked")
    rows = read_jsonl_exact(path, "remediation attempt ledger")
    if len(rows) not in {1, 2}:
        raise ArtifactError("remediation attempt ledger must have one or two entries")
    expected_events = ["execution_authorized", "rerun_observed"]
    for index, row in enumerate(rows):
        if (
            row.get("schema_version") != SCHEMA_VERSION
            or row.get("mode") != REMEDIATION_LEDGER_MODE
            or row.get("sequence") != index + 1
            or row.get("event") != expected_events[index]
        ):
            raise ArtifactError("remediation attempt ledger is not canonical/append-only")
        _validate_hashed_object(row, "entry_sha256", "remediation ledger entry")
    return rows


def load_remediation_execution_authorization(
    authorization_path: Path,
    raw_checkpoint_dir: Path,
    target_id: str,
) -> dict[str, Any]:
    if authorization_path.is_symlink() or not authorization_path.is_file():
        raise ArtifactError("remediation execution authorization is missing or symlinked")
    attempt_root = authorization_path.parent.resolve(strict=True)
    if authorization_path.resolve(strict=True) != attempt_root / "authorization.json":
        raise ArtifactError("authorization is not at the canonical one-attempt path")
    token_path = attempt_root / "authorization-token.json"
    provenance_path = attempt_root / "authorized-run-provenance.json"
    ledger_path = attempt_root / "attempt-ledger.jsonl"
    manifest_path = attempt_root / "rerun-manifest.json"
    token = verify_remediation_token_self(token_path)
    authorization = read_json(authorization_path, "remediation execution authorization")
    if not isinstance(authorization, dict):
        raise ArtifactError("remediation execution authorization must be an object")
    _validate_hashed_object(
        authorization, "authorization_sha256", "remediation execution authorization"
    )
    if (
        authorization.get("schema_version") != SCHEMA_VERSION
        or authorization.get("mode") != REMEDIATION_AUTHORIZATION_MODE
        or authorization.get("binding") != _authorization_binding_from_token(token)
        or authorization.get("token_sha256") != token.get("token_sha256")
        or authorization.get("token_file_sha256") != sha256_file(token_path)
        or authorization.get("authorized_provenance_sha256")
        != sha256_file(provenance_path)
        or authorization.get("canonical_paths") != token.get("canonical_paths")
        or token.get("target_id") != target_id
        or token.get("raw_n_100", {}).get("checkpoint_sha256")
        != sha256_file(raw_checkpoint_dir / "checkpoint.json")
        or token.get("canonical_paths", {}).get("attempt_root") != str(attempt_root)
        or token.get("canonical_paths", {}).get("authorization")
        != str(authorization_path)
        or token.get("canonical_paths", {}).get("ledger") != str(ledger_path)
        or token.get("canonical_paths", {}).get("rerun")
        != str(attempt_root / "rerun")
        or token.get("canonical_paths", {}).get("bundle")
        != str(attempt_root / "bundle")
    ):
        raise ArtifactError("remediation execution authorization binding is invalid")
    if (
        manifest_path.is_symlink()
        or manifest_ids(manifest_path, expected_count=1) != [target_id]
        or sha256_file(manifest_path) != token["rerun"]["manifest_sha256"]
        or manifest_path.stat().st_size != token["rerun"]["manifest_bytes"]
    ):
        raise ArtifactError("authorized canonical rerun manifest is invalid")
    prospective = read_json(provenance_path, "authorized rerun provenance")
    if (
        not isinstance(prospective, dict)
        or prospective.get("fingerprint") != authorization.get("rerun_fingerprint")
    ):
        raise ArtifactError("authorized rerun provenance/fingerprint is invalid")
    rows = _read_remediation_ledger(ledger_path)
    first = rows[0]
    if (
        first.get("authorization_sha256")
        != authorization["authorization_sha256"]
        or first.get("authorization_file_sha256") != sha256_file(authorization_path)
        or first.get("binding") != authorization["binding"]
        or first.get("rerun_fingerprint") != authorization["rerun_fingerprint"]
    ):
        raise ArtifactError("authorized ledger entry does not bind the authorization")
    return {
        "attempt_root": attempt_root,
        "token": token,
        "token_path": token_path,
        "authorization": authorization,
        "authorization_path": authorization_path,
        "authorized_provenance": prospective,
        "authorized_provenance_path": provenance_path,
        "ledger": rows,
        "ledger_path": ledger_path,
        "rerun_root": attempt_root / "rerun",
        "bundle_root": attempt_root / "bundle",
    }


def remediation_rerun_schema(target_id: str) -> tuple[set[str], set[str]]:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", target_id)
    run_prefix = f"runs/{slug}"
    expected_files = {
        "manifest.json",
        "run_meta.json",
        "run_provenance.json",
        "predictions.jsonl",
        "driver_summary.json",
        "watchdog.log",
        f"predictions-audit/{slug}.json",
        f"{run_prefix}/run_record.json",
        f"{run_prefix}/prediction.jsonl",
        f"{run_prefix}/prediction-audit/{slug}.json",
        f"{run_prefix}/query-plan.json",
        f"{run_prefix}/runner.log",
    }
    expected_directories = {
        "predictions-audit",
        "runs",
        run_prefix,
        f"{run_prefix}/prediction-audit",
    }
    return expected_files, expected_directories


def validate_remediation_rerun_tree(
    rerun_root: Path,
    target_id: str,
) -> dict[str, Path]:
    expected_files, expected_directories = remediation_rerun_schema(target_id)
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    if rerun_root.is_symlink() or not rerun_root.is_dir():
        raise ArtifactError("canonical remediation rerun root is missing or symlinked")
    for path in rerun_root.rglob("*"):
        relative = path.relative_to(rerun_root).as_posix()
        if path.is_symlink():
            raise ArtifactError(f"remediation rerun tree contains symlink: {relative}")
        if path.is_file():
            actual_files.add(relative)
        elif path.is_dir():
            actual_directories.add(relative)
        else:
            raise ArtifactError(f"remediation rerun tree contains special file: {relative}")
    if actual_files != expected_files or actual_directories != expected_directories:
        missing_files = sorted(expected_files - actual_files)
        extra_files = sorted(actual_files - expected_files)
        missing_dirs = sorted(expected_directories - actual_directories)
        extra_dirs = sorted(actual_directories - expected_directories)
        raise ArtifactError(
            "remediation rerun tree is not the exhaustive fresh schema; "
            f"missing_files={missing_files}, extra_files={extra_files}, "
            f"missing_dirs={missing_dirs}, extra_dirs={extra_dirs}"
        )
    return {name: rerun_root / name for name in sorted(expected_files)}


def _contains_resume_evidence(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            folded = str(key).casefold()
            if folded in {"resume", "resumed", "skipped", "resume_from"}:
                if item not in (None, False, 0, ""):
                    return True
            if _contains_resume_evidence(item):
                return True
    elif isinstance(value, list):
        return any(_contains_resume_evidence(item) for item in value)
    return False


def validate_remediation_rerun(
    raw_checkpoint_dir: Path,
    rerun_root: Path,
    target_id: str,
    authorization_path: Path,
) -> dict[str, Any]:
    authorization = load_remediation_execution_authorization(
        authorization_path, raw_checkpoint_dir, target_id
    )
    if rerun_root.resolve(strict=True) != authorization["rerun_root"]:
        raise ArtifactError("rerun is not at the authorization's canonical one-attempt path")
    tree_files = validate_remediation_rerun_tree(rerun_root, target_id)
    ids = manifest_ids(rerun_root / "manifest.json", expected_count=1)
    if ids != [target_id]:
        raise ArtifactError("remedial manifest is not exactly the predeclared target ID")
    token = authorization["token"]
    if (
        sha256_file(rerun_root / "manifest.json")
        != token["rerun"]["manifest_sha256"]
        or (rerun_root / "manifest.json").stat().st_size
        != token["rerun"]["manifest_bytes"]
    ):
        raise ArtifactError("executed rerun manifest differs from authorization")

    terminals = terminal_records(rerun_root, ids)
    if set(terminals) != {target_id}:
        raise ArtifactError("remediation rerun does not have one exact terminal record")
    terminal = terminals[target_id]
    if (
        terminal.get("status") != "success"
        or terminal.get("failure_kind") is not None
        or terminal.get("failure_path") is not None
        or terminal.get("validation_mode") != "hash_bound_terminal_v1"
        or terminal["prediction"].get("harness_failure")
    ):
        raise ArtifactError("remediation rerun target is not terminal and successful")

    merged_predictions = index_exact_rows(
        read_jsonl(rerun_root / "predictions.jsonl", "remediation predictions"),
        ids,
        "remediation predictions",
    )
    if merged_predictions[target_id] != terminal["prediction"]:
        raise ArtifactError("remediation merged prediction was cherry-picked or changed")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", target_id)
    root_audit = read_json(
        rerun_root / "predictions-audit" / f"{slug}.json",
        "remediation merged audit",
    )
    task_audit = read_json(
        rerun_root / "runs" / slug / "prediction-audit" / f"{slug}.json",
        "remediation task audit",
    )
    if root_audit != task_audit:
        raise ArtifactError("remediation merged audit differs from terminal audit")

    run_meta = read_json(rerun_root / "run_meta.json", "remediation run metadata")
    run_provenance = read_json(
        rerun_root / "run_provenance.json", "remediation run provenance"
    )
    driver = read_json(rerun_root / "driver_summary.json", "remediation driver summary")
    if any(not isinstance(value, dict) for value in (run_meta, run_provenance, driver)):
        raise ArtifactError("remediation metadata/provenance/driver must be objects")
    if (
        run_meta.get("run_id") != token["rerun"]["run_id"]
        or run_provenance != authorization["authorized_provenance"]
        or sha256_file(rerun_root / "run_provenance.json")
        != authorization["authorization"]["authorized_provenance_sha256"]
        or run_provenance.get("fingerprint")
        != authorization["authorization"]["rerun_fingerprint"]
        or _contains_resume_evidence(run_meta)
        or _contains_resume_evidence(run_provenance)
        or _contains_resume_evidence(driver)
    ):
        raise ArtifactError("remediation execution is not the exact authorized fresh run")
    driver_records, _ = validate_driver_summary(driver, ids, merged_predictions)
    fingerprint = run_provenance.get("fingerprint")
    record = driver_records[target_id]
    if (
        driver.get("run_fingerprint") != fingerprint
        or driver.get("concurrency") != 1
        or record.get("status") != "success"
        or "skipped" in record
        or record.get("returncode") != 0
        or record.get("seconds") != terminal.get("seconds")
    ):
        raise ArtifactError("remediation driver summary does not bind one fresh success")
    if timeout_ids_from_log(rerun_root / "watchdog.log", {target_id}):
        raise ArtifactError("successful remediation rerun still has a watchdog timeout")

    parity = validate_remediation_parity(raw_checkpoint_dir, rerun_root)
    tree_hashes = {name: sha256_file(path) for name, path in tree_files.items()}
    return {
        "target_id": target_id,
        "terminal": terminal,
        "prediction": terminal["prediction"],
        "parity": parity,
        "tree_files": tree_files,
        "tree_hashes": tree_hashes,
        "tree_sha256": canonical_sha256(tree_hashes),
        "authorization": authorization,
    }


def bind_rerun_observation_to_ledger(rerun: dict[str, Any]) -> list[dict[str, Any]]:
    authorization = rerun["authorization"]
    ledger_path = authorization["ledger_path"]
    rows = _read_remediation_ledger(ledger_path)
    terminal = rerun["terminal"]
    observed_core = {
        "schema_version": SCHEMA_VERSION,
        "mode": REMEDIATION_LEDGER_MODE,
        "sequence": 2,
        "event": "rerun_observed",
        "authorization_sha256": authorization["authorization"][
            "authorization_sha256"
        ],
        "rerun_fingerprint": authorization["authorization"]["rerun_fingerprint"],
        "rerun_tree_sha256": rerun["tree_sha256"],
        "run_record_sha256": terminal["run_record_sha256"],
        "prediction_sha256": terminal["prediction_sha256"],
        "audit_sha256": terminal["audit_sha256"],
        "query_plan_sha256": terminal["query_plan_sha256"],
    }
    observed = {
        **observed_core,
        "entry_sha256": canonical_sha256(observed_core),
    }
    if len(rows) == 1:
        with ledger_path.open("ab") as handle:
            handle.write(jsonl_bytes([observed]))
            handle.flush()
            os.fsync(handle.fileno())
        rows = _read_remediation_ledger(ledger_path)
    if len(rows) != 2 or rows[1] != observed:
        raise ArtifactError("rerun observation ledger entry is absent or changed")
    return rows


def validate_canonical_attempt_layout(
    authorization: dict[str, Any],
    *,
    bundle_may_exist: bool,
) -> None:
    attempt_root = authorization["attempt_root"]
    expected = {
        "authorization-token.json",
        "rerun-manifest.json",
        "authorized-run-provenance.json",
        "authorization.json",
        "attempt-ledger.jsonl",
        "rerun",
    }
    if bundle_may_exist:
        expected.add("bundle")
    actual = {path.name for path in attempt_root.iterdir()}
    recovery_names = {
        "packaging-retry-attestation.json",
        "packaging-retry-consumed.json",
        "packaging-retry-evaluation",
    }
    recovery_actual = actual & recovery_names
    valid_recovery_layouts = {
        frozenset(),
        frozenset({"packaging-retry-attestation.json"}),
        frozenset(
            {
                "packaging-retry-attestation.json",
                "packaging-retry-consumed.json",
            }
        ),
        frozenset(recovery_names),
    }
    if (
        frozenset(recovery_actual) not in valid_recovery_layouts
        or actual - recovery_actual != expected
        or ("bundle" in actual and recovery_actual not in (set(), recovery_names))
    ):
        raise ArtifactError(
            "canonical remediation attempt contains partial, stale, or second-attempt "
            f"residue; missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def load_n_100_remediation_state(
    control_root: Path,
    checkpoint_root: Path,
    candidate_root: Path,
) -> dict[str, Any]:
    raw_report = checkpoint_comparison(
        control_root,
        checkpoint_root,
        FULL_MANIFEST_SIZE,
        candidate_root,
    )
    chain = load_checkpoint_chain(checkpoint_root)
    selected = [
        checkpoint
        for checkpoint in chain
        if len(checkpoint["ids"]) <= FULL_MANIFEST_SIZE
    ]
    if (
        len(selected) != FULL_MANIFEST_SIZE // CHECKPOINT_SIZE
        or len(selected[-1]["ids"]) != FULL_MANIFEST_SIZE
    ):
        raise ArtifactError("remediation requires the complete raw N=100 checkpoint chain")
    control = load_run(control_root, expected_count=FULL_MANIFEST_SIZE)
    control_predictions: dict[str, dict[str, Any]] = {}
    candidate_predictions: dict[str, dict[str, Any]] = {}
    control_results: dict[str, dict[str, Any]] = {}
    candidate_results: dict[str, dict[str, Any]] = {}
    evaluator_hashes: set[str] = set()
    runtime_hashes: set[str] = set()
    for checkpoint in selected:
        batch = load_sealed_batch(checkpoint)
        evaluator_hashes.add(batch["evaluator_sha256"])
        runtime_hashes.add(batch["runtime_manifest_sha256"])
        for instance_id in batch["ids"]:
            if instance_id in candidate_predictions:
                raise ArtifactError("raw N=100 remediation state repeats a task")
            control_predictions[instance_id] = batch["control_predictions"][instance_id]
            candidate_predictions[instance_id] = batch["candidate_predictions"][instance_id]
            control_results[instance_id] = batch["control_results"][instance_id]
            candidate_results[instance_id] = batch["candidate_results"][instance_id]
    if len(evaluator_hashes) != 1 or len(runtime_hashes) != 1:
        raise ArtifactError("raw N=100 evaluator/runtime identity is not singular")
    ids = selected[-1]["ids"]
    if list(candidate_predictions) != ids:
        raise ArtifactError("raw N=100 remediation state is not gap-free and ordered")
    completion = read_json(selected[-1]["dir"] / "completion-records.json")
    candidate_records = completion.get("per_instance") if isinstance(completion, dict) else None
    if not isinstance(candidate_records, dict) or set(candidate_records) != set(ids):
        raise ArtifactError("raw N=100 completion records are incomplete")
    control_records = {instance_id: control["records"][instance_id] for instance_id in ids}
    languages = {instance_id: control["languages"][instance_id] for instance_id in ids}
    control_synthetic = synthetic_run(
        ids,
        control_predictions,
        control_results,
        languages,
        control_records,
        control_root / "watchdog.log",
    )
    raw_candidate = synthetic_run(
        ids,
        candidate_predictions,
        candidate_results,
        languages,
        candidate_records,
        selected[-1]["dir"] / "watchdog.log",
    )
    if (
        compare_metric_set(ids, control_synthetic, raw_candidate)
        != raw_report["cumulative"]
        or raw_candidate["reliability"]
        != raw_report["reliability"]["cumulative"]["candidate"]
    ):
        raise ArtifactError("raw N=100 report does not reconcile to its sealed batches")
    return {
        "ids": ids,
        "chain": selected,
        "latest": selected[-1],
        "control": control_synthetic,
        "raw_candidate": raw_candidate,
        "raw_report": raw_report,
        "languages": languages,
        "evaluator_sha256": next(iter(evaluator_hashes)),
        "runtime_manifest_sha256": next(iter(runtime_hashes)),
        "checkpoint_bindings": [
            remediation_checkpoint_binding_v2(checkpoint) for checkpoint in selected
        ],
    }


def watchdog_without_target(path: Path, target_id: str) -> tuple[bytes, int]:
    output: list[bytes] = []
    removed = 0
    for line in path.read_bytes().splitlines(keepends=True):
        decoded = line.decode("utf-8", errors="replace")
        match = re.search(r"(?:slug|instance_id)=([^\s]+)", decoded)
        if match and match.group(1) == target_id:
            removed += 1
        else:
            output.append(line)
    if removed != 1:
        raise ArtifactError(
            "remediation must remove exactly one pre-attested target watchdog event"
        )
    return b"".join(output), removed


def _remediation_subset_run(
    subset_ids: Sequence[str],
    run: dict[str, Any],
    watchdog: Path,
) -> dict[str, Any]:
    return synthetic_run(
        subset_ids,
        {instance_id: run["predictions"][instance_id] for instance_id in subset_ids},
        {instance_id: run["results"][instance_id] for instance_id in subset_ids},
        {instance_id: run["languages"][instance_id] for instance_id in subset_ids},
        {instance_id: run["records"][instance_id] for instance_id in subset_ids},
        watchdog,
    )


def remediation_validation_summary(
    removed_watchdog_events: int,
    official_evaluator_invocations: int,
) -> dict[str, Any]:
    if official_evaluator_invocations not in (1, 2):
        raise ArtifactError("remediation evaluator invocation count is invalid")
    return {
        "predeclared_target_count": 1,
        "raw_history_rewritten": False,
        "non_target_tasks_byte_identical": True,
        "raw_n_100_reconciled": True,
        "same_product_model_policy_evaluator_runtime": True,
        "resource_parity_scope": (
            "recorded metadata only; exact physical-host equivalence is not asserted"
        ),
        "rerun_terminal_success": True,
        "post_n_100_execution_authorized": True,
        "append_only_single_attempt_ledger": True,
        "complete_rerun_tree_sealed": True,
        "official_evaluator_invocations": official_evaluator_invocations,
        "removed_pre_attested_watchdog_events": removed_watchdog_events,
        "pass_at_1": None,
    }


def recompute_raw_n_100_gate(
    ids: Sequence[str],
    latest_batch_ids: Sequence[str],
    control: dict[str, Any],
    raw_candidate: dict[str, Any],
    control_watchdog: Path,
    raw_watchdog: Path,
) -> dict[str, Any]:
    cumulative = compare_metric_set(ids, control, raw_candidate)
    cumulative_languages = per_language_comparison(ids, control, raw_candidate)
    cumulative_gate = release_gate(
        cumulative,
        cumulative_languages,
        control["reliability"],
        raw_candidate["reliability"],
    )
    cumulative_gate["provisional"] = False
    latest_control = _remediation_subset_run(
        latest_batch_ids, control, control_watchdog
    )
    latest_raw = _remediation_subset_run(
        latest_batch_ids, raw_candidate, raw_watchdog
    )
    latest_comparison = compare_metric_set(
        latest_batch_ids, latest_control, latest_raw
    )
    latest_languages = per_language_comparison(
        latest_batch_ids, latest_control, latest_raw
    )
    latest_gate = provisional_gate(
        latest_comparison,
        latest_languages,
        latest_control["reliability"],
        latest_raw["reliability"],
    )
    return compose_checkpoint_gate(
        FULL_MANIFEST_SIZE,
        cumulative_gate,
        latest_gate,
        {"sealed_n_100_reconciliation": True},
    )


def recompute_remediation_assessment(
    ids: Sequence[str],
    originating_batch_ids: Sequence[str],
    latest_batch_ids: Sequence[str],
    control: dict[str, Any],
    raw_candidate: dict[str, Any],
    remediated_candidate: dict[str, Any],
    control_watchdog: Path,
    raw_watchdog: Path,
    remediated_watchdog: Path,
) -> dict[str, Any]:
    if (
        len(ids) != FULL_MANIFEST_SIZE
        or len(originating_batch_ids) != CHECKPOINT_SIZE
        or len(latest_batch_ids) != CHECKPOINT_SIZE
        or not set(originating_batch_ids) <= set(ids)
        or not set(latest_batch_ids) <= set(ids)
    ):
        raise ArtifactError("remediation assessment batch/cumulative universes are invalid")

    cumulative = compare_metric_set(ids, control, remediated_candidate)
    cumulative_languages = per_language_comparison(ids, control, remediated_candidate)
    cumulative_gate = release_gate(
        cumulative,
        cumulative_languages,
        control["reliability"],
        remediated_candidate["reliability"],
    )

    origin_control = _remediation_subset_run(
        originating_batch_ids, control, control_watchdog
    )
    origin_remediated = _remediation_subset_run(
        originating_batch_ids, remediated_candidate, remediated_watchdog
    )
    origin_comparison = compare_metric_set(
        originating_batch_ids, origin_control, origin_remediated
    )
    origin_languages = per_language_comparison(
        originating_batch_ids, origin_control, origin_remediated
    )
    origin_gate = provisional_gate(
        origin_comparison,
        origin_languages,
        origin_control["reliability"],
        origin_remediated["reliability"],
    )

    latest_control = _remediation_subset_run(latest_batch_ids, control, control_watchdog)
    latest_remediated = _remediation_subset_run(
        latest_batch_ids, remediated_candidate, remediated_watchdog
    )
    latest_comparison = compare_metric_set(
        latest_batch_ids, latest_control, latest_remediated
    )
    latest_languages = per_language_comparison(
        latest_batch_ids, latest_control, latest_remediated
    )
    latest_gate = provisional_gate(
        latest_comparison,
        latest_languages,
        latest_control["reliability"],
        latest_remediated["reliability"],
    )
    passed = bool(
        cumulative_gate["passed"] and origin_gate["passed"] and latest_gate["passed"]
    )
    gate = {
        "passed": passed,
        "provisional": False,
        "release_eligible": passed,
        "checks": {
            "cumulative_release_gate": cumulative_gate["checks"],
            "repaired_originating_batch_10": origin_gate["checks"],
            "latest_batch_10": latest_gate["checks"],
        },
        "failure_reasons": (
            [
                f"cumulative:{reason}"
                for reason in cumulative_gate["failure_reasons"]
            ]
            + [
                f"repaired_originating_batch_10:{reason}"
                for reason in origin_gate["failure_reasons"]
            ]
            + [
                f"latest_batch_10:{reason}"
                for reason in latest_gate["failure_reasons"]
            ]
        ),
        "cumulative_gate": cumulative_gate,
        "repaired_originating_batch_gate": origin_gate,
        "latest_batch_gate": latest_gate,
    }
    raw_gate = recompute_raw_n_100_gate(
        ids,
        latest_batch_ids,
        control,
        raw_candidate,
        control_watchdog,
        raw_watchdog,
    )
    return {
        "comparison": {
            "control_vs_raw": compare_metric_set(ids, control, raw_candidate),
            "control_vs_remediated": cumulative,
            "raw_vs_remediated": compare_metric_set(
                ids, raw_candidate, remediated_candidate
            ),
            "repaired_originating_batch_10_control_vs_remediated": origin_comparison,
            "latest_batch_10_control_vs_remediated": latest_comparison,
        },
        "per_language": {
            "cumulative": cumulative_languages,
            "repaired_originating_batch_10": origin_languages,
            "latest_batch_10": latest_languages,
        },
        "reliability": {
            "cumulative": {
                "control": control["reliability"],
                "raw_candidate": raw_candidate["reliability"],
                "remediated_candidate": remediated_candidate["reliability"],
            },
            "repaired_originating_batch_10": {
                "control": origin_control["reliability"],
                "remediated_candidate": origin_remediated["reliability"],
            },
            "latest_batch_10": {
                "control": latest_control["reliability"],
                "remediated_candidate": latest_remediated["reliability"],
            },
        },
        "raw_gate": raw_gate,
        "gate": gate,
    }


def _legacy_verify_remediation_bundle(root: Path) -> dict[str, Any]:
    seal = read_json(root / "remediation.json", "remediation seal")
    if (
        not isinstance(seal, dict)
        or seal.get("schema_version") != SCHEMA_VERSION
        or seal.get("mode") != "false-kill-remediation-overlay"
    ):
        raise ArtifactError("invalid remediation bundle seal")
    target_ids = seal.get("target_ids")
    if (
        not isinstance(target_ids, list)
        or len(target_ids) != 1
        or not isinstance(target_ids[0], str)
        or not target_ids[0]
    ):
        raise ArtifactError("remediation bundle must contain exactly one target ID")
    target_id = target_ids[0]
    sealed_files = seal.get("sealed_files")
    if not isinstance(sealed_files, dict) or not sealed_files:
        raise ArtifactError("remediation bundle has no immutable file inventory")
    actual_files: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ArtifactError("remediation bundle may not contain symlinks")
        if path.is_file():
            actual_files.add(str(path.relative_to(root)))
    if actual_files != set(sealed_files) | {"remediation.json"}:
        raise ArtifactError("remediation bundle contains unsealed or missing files")
    for name, expected_hash in sealed_files.items():
        if not isinstance(name, str) or not isinstance(expected_hash, str):
            raise ArtifactError("remediation sealed file inventory is malformed")
        if sha256_file(root / name) != expected_hash:
            raise ArtifactError(f"immutable remediation artifact changed: {name}")

    binding = seal.get("bundle_binding")
    if (
        not isinstance(binding, dict)
        or seal.get("bundle_binding_sha256") != canonical_sha256(binding)
        or binding.get("sealed_files") != sealed_files
        or binding.get("target_ids") != target_ids
        or binding.get("declaration_sha256")
        != sha256_file(root / "declaration.json")
        or binding.get("comparison_sha256")
        != sha256_file(root / "comparison.json")
        or binding.get("raw_checkpoint_chain_sha256")
        != sha256_file(root / "raw-checkpoint-chain.json")
        or binding.get("raw_n_100_run_meta_sha256")
        != sha256_file(root / "raw-n-100-run/run_meta.json")
        or binding.get("raw_n_100_run_provenance_sha256")
        != sha256_file(root / "raw-n-100-run/run_provenance.json")
        or binding.get("runtime_manifest_sha256")
        != sha256_file(root / "official-evaluation/runtime-manifest.json")
    ):
        raise ArtifactError("remediation bundle binding is invalid")
    raw_chain = read_json(
        root / "raw-checkpoint-chain.json", "raw checkpoint chain binding"
    )
    if not isinstance(raw_chain, list) or any(
        not isinstance(row, dict) for row in raw_chain
    ):
        raise ArtifactError("remediation raw checkpoint chain must be a list of objects")
    if (
        [row.get("count") for row in raw_chain]
        != list(range(CHECKPOINT_SIZE, FULL_MANIFEST_SIZE + 1, CHECKPOINT_SIZE))
        or any(
            row.get("runtime_manifest_sha256")
            != binding.get("runtime_manifest_sha256")
            or row.get("evaluator_sha256") != binding.get("evaluator_sha256")
            for row in raw_chain
        )
        or raw_chain[-1].get("checkpoint_sha256")
        != binding.get("raw_n_100_checkpoint_sha256")
        or raw_chain[-1].get("run_meta_sha256")
        != binding.get("raw_n_100_run_meta_sha256")
        or raw_chain[-1].get("run_provenance_sha256")
        != binding.get("raw_n_100_run_provenance_sha256")
    ):
        raise ArtifactError("remediation raw N=100 chain binding is invalid")
    sealed_chain_ids: list[str] = []
    sealed_control_predictions: dict[str, dict[str, Any]] = {}
    sealed_candidate_predictions: dict[str, dict[str, Any]] = {}
    sealed_control_results: dict[str, dict[str, Any]] = {}
    sealed_candidate_results: dict[str, dict[str, Any]] = {}
    for row in raw_chain:
        count = row["count"]
        batch_root = root / "sealed-batches" / f"checkpoint-{count:04d}"
        batch_manifest = batch_root / "batch-manifest.json"
        control_prediction_path = batch_root / "control-predictions.jsonl"
        candidate_prediction_path = batch_root / "candidate-predictions.jsonl"
        control_result_path = batch_root / "control-results.jsonl"
        candidate_result_path = batch_root / "candidate-results.jsonl"
        if (
            sha256_file(batch_manifest) != row.get("batch_manifest_sha256")
            or sha256_file(control_prediction_path)
            != row.get("control_batch_predictions_sha256")
            or sha256_file(candidate_prediction_path)
            != row.get("candidate_batch_predictions_sha256")
            or sha256_file(control_result_path) != row.get("control_results_sha256")
            or sha256_file(candidate_result_path) != row.get("candidate_results_sha256")
        ):
            raise ArtifactError(f"sealed checkpoint-{count:04d} paired inputs changed")
        batch_ids = manifest_ids(batch_manifest, expected_count=CHECKPOINT_SIZE)
        if set(batch_ids) & set(sealed_chain_ids):
            raise ArtifactError("sealed remediation batch chain repeats a task")
        sealed_chain_ids.extend(batch_ids)
        sealed_control_predictions.update(
            index_exact_rows(
                read_jsonl(control_prediction_path, "sealed control batch predictions"),
                batch_ids,
                "sealed control batch predictions",
            )
        )
        sealed_candidate_predictions.update(
            index_exact_rows(
                read_jsonl(
                    candidate_prediction_path, "sealed candidate batch predictions"
                ),
                batch_ids,
                "sealed candidate batch predictions",
            )
        )
        sealed_control_results.update(
            index_exact_rows(
                read_jsonl(control_result_path, "sealed control batch results"),
                batch_ids,
                "sealed control batch results",
            )
        )
        sealed_candidate_results.update(
            index_exact_rows(
                read_jsonl(candidate_result_path, "sealed candidate batch results"),
                batch_ids,
                "sealed candidate batch results",
            )
        )
    runtime = read_json(
        root / "official-evaluation/runtime-manifest.json",
        "remediation evaluator runtime",
    )
    evaluator_identity = runtime.get("evaluator") if isinstance(runtime, dict) else None
    evaluator_copy = root / "official-evaluation/evaluate.py"
    sealed_evaluator = seal.get("evaluator")
    if (
        not isinstance(evaluator_identity, dict)
        or not isinstance(sealed_evaluator, dict)
        or not evaluator_copy.is_file()
        or evaluator_identity.get("sha256") != binding.get("evaluator_sha256")
        or sha256_file(evaluator_copy) != binding.get("evaluator_sha256")
        or sealed_evaluator.get("sha256") != binding.get("evaluator_sha256")
        or sealed_evaluator.get("runtime_manifest_sha256")
        != binding.get("runtime_manifest_sha256")
        or sealed_evaluator.get("invocations") != 1
    ):
        raise ArtifactError("remediation evaluator/runtime identity is invalid")
    receipt = seal.get("execution_receipt")
    if (
        not isinstance(receipt, dict)
        or receipt.get("returncode") != 0
        or receipt.get("prediction_sha256")
        != sha256_file(root / "official-evaluation/prediction.jsonl")
        or receipt.get("result_sha256")
        != sha256_file(root / "official-evaluation/result.jsonl")
        or receipt.get("stdout_sha256")
        != sha256_file(root / "official-evaluation/stdout.log")
        or receipt.get("stderr_sha256")
        != sha256_file(root / "official-evaluation/stderr.log")
        or binding.get("execution_receipt_sha256") != canonical_sha256(receipt)
        or seal.get("evaluator_worktree_isolation")
        != REMEDIATION_EVALUATOR_WORKTREE_ISOLATION
    ):
        raise ArtifactError("remediation official evaluator receipt is invalid")

    declaration = read_json(root / "declaration.json", "sealed declaration")
    if (
        not isinstance(declaration, dict)
        or declaration.get("target_ids") != target_ids
        or declaration.get("declaration_sha256")
        != canonical_sha256(_drop_keys(declaration, {"declaration_sha256"}))
    ):
        raise ArtifactError("sealed remediation declaration is invalid")
    ids = manifest_ids(root / "manifest.json", expected_count=FULL_MANIFEST_SIZE)
    raw_predictions = index_exact_rows(
        read_jsonl(root / "raw-candidate-predictions.jsonl", "raw predictions"),
        ids,
        "raw predictions",
    )
    remediated_predictions = index_exact_rows(
        read_jsonl(
            root / "remediated-candidate-predictions.jsonl",
            "remediated predictions",
        ),
        ids,
        "remediated predictions",
    )
    raw_results = index_exact_rows(
        read_jsonl(root / "raw-candidate-results.jsonl", "raw results"),
        ids,
        "raw results",
    )
    remediated_results = index_exact_rows(
        read_jsonl(root / "remediated-candidate-results.jsonl", "remediated results"),
        ids,
        "remediated results",
    )
    raw_records = read_json(
        root / "raw-candidate-records.json", "raw candidate records"
    )
    remediated_records = read_json(
        root / "remediated-candidate-records.json",
        "remediated candidate records",
    )
    if (
        not isinstance(raw_records, dict)
        or not isinstance(remediated_records, dict)
        or set(raw_records) != set(ids)
        or set(remediated_records) != set(ids)
    ):
        raise ArtifactError("remediation record universes differ from the manifest")
    prediction_changes = [
        instance_id
        for instance_id in ids
        if raw_predictions[instance_id] != remediated_predictions[instance_id]
    ]
    result_changes = [
        instance_id
        for instance_id in ids
        if raw_results[instance_id] != remediated_results[instance_id]
    ]
    record_changes = [
        instance_id
        for instance_id in ids
        if raw_records[instance_id] != remediated_records[instance_id]
    ]
    if (
        prediction_changes != [target_id]
        or result_changes != [target_id]
        or record_changes != [target_id]
    ):
        raise ArtifactError("remediation changed a task outside the predeclared target")
    official_prediction = index_exact_rows(
        read_jsonl(
            root / "official-evaluation/prediction.jsonl",
            "official target prediction",
        ),
        [target_id],
        "official target prediction",
    )[target_id]
    official_result = index_exact_rows(
        read_jsonl(root / "official-evaluation/result.jsonl", "official target result"),
        [target_id],
        "official target result",
    )[target_id]
    if (
        official_prediction != remediated_predictions[target_id]
        or official_result != remediated_results[target_id]
        or official_result.get("error")
    ):
        raise ArtifactError("remediation target official evaluation was not successful")
    comparison = read_json(root / "comparison.json", "remediation comparison")
    validation = comparison.get("validation") if isinstance(comparison, dict) else None
    if (
        not isinstance(comparison, dict)
        or comparison.get("target_ids") != target_ids
        or not isinstance(validation, dict)
        or validation.get("non_target_tasks_byte_identical") is not True
        or validation.get("raw_history_rewritten") is not False
    ):
        raise ArtifactError("remediation comparison validation is incomplete")
    reliability = comparison.get("reliability")
    if not isinstance(reliability, dict):
        raise ArtifactError("remediation comparison has no reliability object")
    raw_reliability = reliability.get("raw_candidate")
    remediated_reliability = reliability.get("remediated_candidate")
    if not isinstance(raw_reliability, dict) or not isinstance(
        remediated_reliability, dict
    ):
        raise ArtifactError("remediation reliability records are malformed")
    recomputed_raw_reliability = reliability_from_records(
        ids,
        raw_predictions,
        raw_results,
        raw_records,
        root / "raw-watchdog.log",
    )
    recomputed_remediated_reliability = reliability_from_records(
        ids,
        remediated_predictions,
        remediated_results,
        remediated_records,
        root / "remediated-watchdog.log",
    )
    if (
        recomputed_raw_reliability != raw_reliability
        or recomputed_remediated_reliability != remediated_reliability
    ):
        raise ArtifactError("remediation reliability does not recompute from sealed inputs")
    for field in ("failure_ids", "timeout_ids", "evaluator_error_ids"):
        raw_ids = set(raw_reliability.get(field, []))
        remediated_ids = set(remediated_reliability.get(field, []))
        if target_id not in raw_ids or remediated_ids != raw_ids - {target_id}:
            raise ArtifactError(
                f"remediation reliability changed IDs other than the target: {field}"
            )
    raw_task_metrics = {
        instance_id: task_metrics(
            instance_id, raw_predictions[instance_id], raw_results[instance_id]
        )
        for instance_id in ids
    }
    remediated_task_metrics = {
        instance_id: task_metrics(
            instance_id,
            remediated_predictions[instance_id],
            remediated_results[instance_id],
        )
        for instance_id in ids
    }
    recomputed_raw_vs_remediated = compare_metric_set(
        ids,
        {"per_task": raw_task_metrics},
        {"per_task": remediated_task_metrics},
    )
    if recomputed_raw_vs_remediated != comparison.get("comparison", {}).get(
        "raw_vs_remediated"
    ):
        raise ArtifactError("raw-vs-remediated metrics do not recompute from sealed inputs")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "verify-remediation",
        "bundle": str(root),
        "target_id": target_id,
        "sealed_files": len(sealed_files),
        "remediated_gate_passed": bool(
            comparison.get("gate", {}).get("passed")
        ),
        "release_eligible": bool(
            comparison.get("gate", {}).get("release_eligible")
        ),
        "remediation_sha256": sha256_file(root / "remediation.json"),
    }


def verify_remediation_bundle(root: Path) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise ArtifactError("remediation bundle is missing, non-directory, or symlinked")
    seal = read_json(root / "remediation.json", "remediation seal")
    if (
        not isinstance(seal, dict)
        or seal.get("schema_version") != SCHEMA_VERSION
        or seal.get("mode") != "false-kill-remediation-overlay"
    ):
        raise ArtifactError("invalid remediation bundle seal")
    target_ids = seal.get("target_ids")
    if (
        not isinstance(target_ids, list)
        or len(target_ids) != 1
        or not isinstance(target_ids[0], str)
        or not target_ids[0]
    ):
        raise ArtifactError("remediation bundle must contain exactly one target ID")
    target_id = target_ids[0]
    sealed_files = seal.get("sealed_files")
    if not isinstance(sealed_files, dict) or not sealed_files:
        raise ArtifactError("remediation bundle has no immutable file inventory")
    actual_files: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ArtifactError("remediation bundle may not contain symlinks")
        if path.is_file():
            actual_files.add(path.relative_to(root).as_posix())
    if actual_files != set(sealed_files) | {"remediation.json"}:
        raise ArtifactError("remediation bundle contains unsealed or missing files")
    for name, expected_hash in sealed_files.items():
        relative = Path(name)
        if (
            not isinstance(name, str)
            or not isinstance(expected_hash, str)
            or relative.is_absolute()
            or ".." in relative.parts
            or sha256_file(root / relative) != expected_hash
        ):
            raise ArtifactError(f"immutable remediation artifact is invalid: {name}")

    binding = seal.get("bundle_binding")
    if (
        not isinstance(binding, dict)
        or seal.get("bundle_binding_sha256") != canonical_sha256(binding)
        or binding.get("sealed_files") != sealed_files
        or binding.get("target_ids") != target_ids
        or binding.get("declaration_sha256")
        != sha256_file(root / "declaration.json")
        or binding.get("authorization_token_sha256")
        != sha256_file(root / "authorization-token.json")
        or binding.get("authorization_sha256")
        != sha256_file(root / "authorization.json")
        or binding.get("attempt_ledger_sha256")
        != sha256_file(root / "attempt-ledger.jsonl")
        or binding.get("comparison_sha256")
        != sha256_file(root / "comparison.json")
        or binding.get("raw_checkpoint_chain_sha256")
        != sha256_file(root / "raw-checkpoint-chain.json")
        or binding.get("raw_n_100_run_meta_sha256")
        != sha256_file(root / "raw-n-100-run/run_meta.json")
        or binding.get("raw_n_100_run_provenance_sha256")
        != sha256_file(root / "raw-n-100-run/run_provenance.json")
        or binding.get("runtime_manifest_sha256")
        != sha256_file(root / "official-evaluation/runtime-manifest.json")
    ):
        raise ArtifactError("remediation bundle binding is invalid")

    raw_chain = read_json(
        root / "raw-checkpoint-chain.json", "raw checkpoint chain binding"
    )
    if (
        not isinstance(raw_chain, list)
        or any(not isinstance(row, dict) for row in raw_chain)
        or [row.get("count") for row in raw_chain]
        != list(range(CHECKPOINT_SIZE, FULL_MANIFEST_SIZE + 1, CHECKPOINT_SIZE))
        or any(
            row.get("runtime_manifest_sha256")
            != binding.get("runtime_manifest_sha256")
            or row.get("evaluator_sha256") != binding.get("evaluator_sha256")
            for row in raw_chain
        )
        or raw_chain[-1].get("checkpoint_sha256")
        != binding.get("raw_n_100_checkpoint_sha256")
        or raw_chain[-1].get("run_meta_sha256")
        != binding.get("raw_n_100_run_meta_sha256")
        or raw_chain[-1].get("run_provenance_sha256")
        != binding.get("raw_n_100_run_provenance_sha256")
    ):
        raise ArtifactError("remediation raw N=100 chain binding is invalid")

    sealed_chain_ids: list[str] = []
    sealed_control_predictions: dict[str, dict[str, Any]] = {}
    sealed_candidate_predictions: dict[str, dict[str, Any]] = {}
    sealed_control_results: dict[str, dict[str, Any]] = {}
    sealed_candidate_results: dict[str, dict[str, Any]] = {}
    for row in raw_chain:
        count = row["count"]
        batch_root = root / "sealed-batches" / f"checkpoint-{count:04d}"
        batch_manifest = batch_root / "batch-manifest.json"
        control_prediction_path = batch_root / "control-predictions.jsonl"
        candidate_prediction_path = batch_root / "candidate-predictions.jsonl"
        control_result_path = batch_root / "control-results.jsonl"
        candidate_result_path = batch_root / "candidate-results.jsonl"
        if (
            sha256_file(batch_manifest) != row.get("batch_manifest_sha256")
            or sha256_file(control_prediction_path)
            != row.get("control_batch_predictions_sha256")
            or sha256_file(candidate_prediction_path)
            != row.get("candidate_batch_predictions_sha256")
            or sha256_file(control_result_path) != row.get("control_results_sha256")
            or sha256_file(candidate_result_path)
            != row.get("candidate_results_sha256")
        ):
            raise ArtifactError(f"sealed checkpoint-{count:04d} paired inputs changed")
        batch_ids = manifest_ids(batch_manifest, expected_count=CHECKPOINT_SIZE)
        if set(batch_ids) & set(sealed_chain_ids):
            raise ArtifactError("sealed remediation batch chain repeats a task")
        sealed_chain_ids.extend(batch_ids)
        sealed_control_predictions.update(
            index_exact_rows(
                read_jsonl(control_prediction_path, "sealed control batch predictions"),
                batch_ids,
                "sealed control batch predictions",
            )
        )
        sealed_candidate_predictions.update(
            index_exact_rows(
                read_jsonl(
                    candidate_prediction_path, "sealed candidate batch predictions"
                ),
                batch_ids,
                "sealed candidate batch predictions",
            )
        )
        sealed_control_results.update(
            index_exact_rows(
                read_jsonl(control_result_path, "sealed control batch results"),
                batch_ids,
                "sealed control batch results",
            )
        )
        sealed_candidate_results.update(
            index_exact_rows(
                read_jsonl(candidate_result_path, "sealed candidate batch results"),
                batch_ids,
                "sealed candidate batch results",
            )
        )

    runtime = read_json(
        root / "official-evaluation/runtime-manifest.json",
        "remediation evaluator runtime",
    )
    evaluator_identity = runtime.get("evaluator") if isinstance(runtime, dict) else None
    evaluator_copy = root / "official-evaluation/evaluate.py"
    sealed_evaluator = seal.get("evaluator")
    receipt = seal.get("execution_receipt")
    recovery_mode = seal.get("packaging_retry_recovery")
    if not isinstance(recovery_mode, bool):
        raise ArtifactError("remediation packaging retry mode is not explicit")
    evaluator_counts = {
        "total_official_evaluator_invocations": 2 if recovery_mode else 1,
        "prior_unsealed_operator_attested": 1 if recovery_mode else 0,
        "sealed_receipts": 1,
        "aws_execution_attempts": 1,
    }
    if (
        not isinstance(evaluator_identity, dict)
        or not isinstance(sealed_evaluator, dict)
        or evaluator_identity.get("sha256") != binding.get("evaluator_sha256")
        or sha256_file(evaluator_copy) != binding.get("evaluator_sha256")
        or sealed_evaluator.get("sha256") != binding.get("evaluator_sha256")
        or sealed_evaluator.get("runtime_manifest_sha256")
        != binding.get("runtime_manifest_sha256")
        or sealed_evaluator.get("invocations")
        != evaluator_counts["total_official_evaluator_invocations"]
        or any(
            sealed_evaluator.get(key) != value
            for key, value in evaluator_counts.items()
        )
        or not isinstance(receipt, dict)
        or receipt.get("returncode") != 0
        or receipt.get("prediction_sha256")
        != sha256_file(root / "official-evaluation/prediction.jsonl")
        or receipt.get("result_sha256")
        != sha256_file(root / "official-evaluation/result.jsonl")
        or receipt.get("stdout_sha256")
        != sha256_file(root / "official-evaluation/stdout.log")
        or receipt.get("stderr_sha256")
        != sha256_file(root / "official-evaluation/stderr.log")
        or binding.get("execution_receipt_sha256") != canonical_sha256(receipt)
        or seal.get("evaluator_worktree_isolation")
        != REMEDIATION_EVALUATOR_WORKTREE_ISOLATION
    ):
        raise ArtifactError("remediation official evaluator identity/receipt is invalid")

    declaration = read_json(root / "declaration.json", "sealed declaration")
    token = verify_remediation_token_self(root / "authorization-token.json")
    authorization = read_json(root / "authorization.json", "sealed authorization")
    if not isinstance(authorization, dict):
        raise ArtifactError("sealed remediation authorization is invalid")
    _validate_hashed_object(
        authorization, "authorization_sha256", "sealed remediation authorization"
    )
    if (
        not isinstance(declaration, dict)
        or declaration.get("target_ids") != target_ids
        or declaration.get("declaration_sha256")
        != canonical_sha256(_drop_keys(declaration, {"declaration_sha256"}))
        or token.get("target_id") != target_id
        or token.get("declaration", {}).get("declaration_sha256")
        != declaration.get("declaration_sha256")
        or token.get("raw_n_100", {}).get("checkpoint_sha256")
        != binding.get("raw_n_100_checkpoint_sha256")
        or token.get("raw_n_100", {}).get("run_provenance_sha256")
        != binding.get("raw_n_100_run_provenance_sha256")
        or authorization.get("binding") != _authorization_binding_from_token(token)
        or authorization.get("token_sha256") != token.get("token_sha256")
        or authorization.get("token_file_sha256")
        != sha256_file(root / "authorization-token.json")
    ):
        raise ArtifactError("sealed declaration/token/authorization binding is invalid")
    ledger = _read_remediation_ledger(root / "attempt-ledger.jsonl")
    if len(ledger) != 2:
        raise ArtifactError("sealed remediation ledger lacks the one observed attempt")
    if (
        ledger[0].get("authorization_sha256")
        != authorization.get("authorization_sha256")
        or ledger[0].get("authorization_file_sha256")
        != sha256_file(root / "authorization.json")
        or ledger[0].get("binding") != authorization.get("binding")
        or ledger[0].get("rerun_fingerprint")
        != authorization.get("rerun_fingerprint")
    ):
        raise ArtifactError("sealed authorization ledger entry is invalid")

    rerun_files = validate_remediation_rerun_tree(root / "rerun", target_id)
    rerun_hashes = {name: sha256_file(path) for name, path in rerun_files.items()}
    if (
        seal.get("rerun_tree", {}).get("files") != rerun_hashes
        or seal.get("rerun_tree", {}).get("sha256") != canonical_sha256(rerun_hashes)
        or binding.get("rerun_tree_sha256") != canonical_sha256(rerun_hashes)
        or ledger[1].get("rerun_tree_sha256") != canonical_sha256(rerun_hashes)
    ):
        raise ArtifactError("sealed complete rerun tree binding is invalid")
    rerun_provenance = read_json(root / "rerun/run_provenance.json")
    raw_run_meta = read_json(
        root / "raw-n-100-run/run_meta.json", "sealed raw N=100 run metadata"
    )
    raw_run_provenance, sealed_raw_provenance_binding = (
        remediation_raw_provenance_binding(
            root / "raw-n-100-run/run_provenance.json"
        )
    )
    if (
        sha256_file(root / "rerun/run_provenance.json")
        != authorization.get("authorized_provenance_sha256")
        or not isinstance(rerun_provenance, dict)
        or rerun_provenance.get("fingerprint")
        != authorization.get("rerun_fingerprint")
        or not isinstance(raw_run_meta, dict)
        or raw_run_meta.get("run_id")
        != token.get("raw_n_100", {}).get("candidate_run_id")
        or sealed_raw_provenance_binding["file_sha256"]
        != token.get("raw_n_100", {}).get("run_provenance_sha256")
        or sealed_raw_provenance_binding["bytes"]
        != token.get("raw_n_100", {}).get("run_provenance_bytes")
        or sealed_raw_provenance_binding["fingerprint"]
        != token.get("raw_n_100", {}).get("candidate_fingerprint")
        or sealed_raw_provenance_binding["policy_semantics"]
        != token.get("raw_n_100", {}).get("policy_semantics")
    ):
        raise ArtifactError("sealed rerun provenance is not the authorized provenance")
    recomputed_parity = validate_remediation_parity(
        root / "raw-n-100-run", root / "rerun"
    )
    if seal.get("parity") != recomputed_parity:
        raise ArtifactError("sealed remediation parity is not independently reproducible")
    rerun_terminal = terminal_records(root / "rerun", [target_id])[target_id]
    if (
        ledger[1].get("authorization_sha256")
        != authorization.get("authorization_sha256")
        or ledger[1].get("rerun_fingerprint")
        != authorization.get("rerun_fingerprint")
        or ledger[1].get("run_record_sha256")
        != rerun_terminal["run_record_sha256"]
        or ledger[1].get("prediction_sha256")
        != rerun_terminal["prediction_sha256"]
        or ledger[1].get("audit_sha256") != rerun_terminal["audit_sha256"]
        or ledger[1].get("query_plan_sha256")
        != rerun_terminal["query_plan_sha256"]
    ):
        raise ArtifactError("sealed rerun observation ledger entry is invalid")

    retry_artifacts = {
        "attestation": root / "packaging-retry-attestation.json",
        "consumption": root / "packaging-retry-consumed.json",
        "evaluation_receipt": root / "packaging-retry-evaluation-receipt.json",
        "packager": root / "packaging-retry-packager.py",
    }
    if recovery_mode:
        if any(path.is_symlink() or not path.is_file() for path in retry_artifacts.values()):
            raise ArtifactError("sealed packaging retry evidence is incomplete")
        attestation = read_json(
            retry_artifacts["attestation"], "sealed packaging retry attestation"
        )
        consumption = read_json(
            retry_artifacts["consumption"], "sealed packaging retry consumption"
        )
        retry_receipt = read_json(
            retry_artifacts["evaluation_receipt"],
            "sealed packaging retry evaluation receipt",
        )
        if not all(
            isinstance(value, dict)
            for value in (attestation, consumption, retry_receipt)
        ):
            raise ArtifactError("sealed packaging retry evidence must be JSON objects")
        _validate_hashed_object(
            attestation,
            "attestation_sha256",
            "sealed packaging retry attestation",
        )
        _validate_hashed_object(
            consumption,
            "consumption_sha256",
            "sealed packaging retry consumption",
        )
        _validate_hashed_object(
            retry_receipt,
            "receipt_sha256",
            "sealed packaging retry evaluation receipt",
        )
        recovery_packager_sha256 = sha256_file(retry_artifacts["packager"])
        expected_attestation = {
            "schema_version": SCHEMA_VERSION,
            "mode": PACKAGING_RETRY_ATTESTATION_MODE,
            "incident_mode": PACKAGING_RETRY_INCIDENT_MODE,
            "target_id": target_id,
            "canonical_attestation_path": str(
                Path(token["canonical_paths"]["attempt_root"])
                / "packaging-retry-attestation.json"
            ),
            "authorization_sha256": authorization["authorization_sha256"],
            "authorization_file_sha256": sha256_file(root / "authorization.json"),
            "attempt_ledger_sha256": sha256_file(root / "attempt-ledger.jsonl"),
            "attempt_ledger_entries": 2,
            "rerun_tree_sha256": canonical_sha256(rerun_hashes),
            "rerun_prediction_sha256": rerun_terminal["prediction_sha256"],
            "evaluator_sha256": binding["evaluator_sha256"],
            "runtime_manifest_sha256": binding["runtime_manifest_sha256"],
            "failed_packager_sha256": PACKAGING_RETRY_FAILED_PACKAGER_SHA256,
            "recovery_packager_sha256": recovery_packager_sha256,
            "operator_attestation": {
                "prior_official_evaluator_succeeded": True,
                "prior_receipt_or_result_survived": False,
                "failure_stage": "post-evaluation-pre-bundle",
                "missing_path": "triage/triage.jsonl",
                "correct_frozen_path": "control-triage.jsonl",
                "correct_frozen_sha256": raw_chain[-1]["triage_sha256"],
                "bundle_absent_at_predeclaration": True,
            },
            "known_counts_before_retry": {
                "official_evaluator_invocations": 1,
                "prior_unsealed_operator_attested": 1,
                "sealed_receipts": 0,
                "aws_execution_attempts": 1,
            },
        }
        if _drop_keys(attestation, {"attestation_sha256"}) != expected_attestation:
            raise ArtifactError("sealed packaging retry attestation binding is invalid")
        expected_consumption = {
            "schema_version": SCHEMA_VERSION,
            "mode": PACKAGING_RETRY_CONSUMPTION_MODE,
            "target_id": target_id,
            "retry_evaluator_invocation_number": 2,
            "attestation_file_sha256": sha256_file(retry_artifacts["attestation"]),
            "attestation_sha256": attestation["attestation_sha256"],
            "authorization_sha256": authorization["authorization_sha256"],
            "attempt_ledger_sha256": sha256_file(root / "attempt-ledger.jsonl"),
            "rerun_tree_sha256": canonical_sha256(rerun_hashes),
            "evaluator_sha256": binding["evaluator_sha256"],
            "runtime_manifest_sha256": binding["runtime_manifest_sha256"],
            "recovery_packager_sha256": recovery_packager_sha256,
            "invocation_accounting_after_consumption": {
                "observed_prior_official_evaluator_invocations": 1,
                "retry_invocation_number_reserved": 2,
                "total_invocation_slots_consumed": 2,
                "maximum_total_official_evaluator_invocations": 2,
                "prior_unsealed_operator_attested": 1,
                "sealed_receipts": 0,
                "aws_execution_attempts": 1,
            },
        }
        if _drop_keys(consumption, {"consumption_sha256"}) != expected_consumption:
            raise ArtifactError("sealed packaging retry consumption binding is invalid")
        expected_retry_receipt = {
            "schema_version": SCHEMA_VERSION,
            "mode": PACKAGING_RETRY_RECEIPT_MODE,
            "target_id": target_id,
            "attestation_file_sha256": sha256_file(retry_artifacts["attestation"]),
            "consumption_file_sha256": sha256_file(retry_artifacts["consumption"]),
            "authorization_sha256": authorization["authorization_sha256"],
            "rerun_tree_sha256": canonical_sha256(rerun_hashes),
            "execution_receipt": receipt,
        }
        if _drop_keys(retry_receipt, {"receipt_sha256"}) != expected_retry_receipt:
            raise ArtifactError(
                "sealed packaging retry evaluation receipt binding is invalid"
            )
        expected_retry_binding = {
            "packaging_retry_attestation_sha256": sha256_file(
                retry_artifacts["attestation"]
            ),
            "packaging_retry_consumption_sha256": sha256_file(
                retry_artifacts["consumption"]
            ),
            "packaging_retry_evaluation_receipt_sha256": sha256_file(
                retry_artifacts["evaluation_receipt"]
            ),
            "recovery_packager_sha256": recovery_packager_sha256,
        }
    else:
        if any(path.exists() or path.is_symlink() for path in retry_artifacts.values()):
            raise ArtifactError("normal remediation bundle contains retry evidence")
        expected_retry_binding = {
            "packaging_retry_attestation_sha256": None,
            "packaging_retry_consumption_sha256": None,
            "packaging_retry_evaluation_receipt_sha256": None,
            "recovery_packager_sha256": None,
        }
    if any(binding.get(key) != value for key, value in expected_retry_binding.items()):
        raise ArtifactError("remediation packaging retry bundle binding is invalid")

    ids = manifest_ids(root / "manifest.json", expected_count=FULL_MANIFEST_SIZE)
    languages = read_json(root / "languages.json", "sealed task languages")
    control_records = read_json(root / "control-records.json", "control records")
    raw_records = read_json(root / "raw-candidate-records.json", "raw records")
    remediated_records = read_json(
        root / "remediated-candidate-records.json", "remediated records"
    )
    if any(
        not isinstance(value, dict) or set(value) != set(ids)
        for value in (languages, control_records, raw_records, remediated_records)
    ) or any(not isinstance(languages[instance_id], str) for instance_id in ids):
        raise ArtifactError("sealed remediation languages/record universes are invalid")
    raw_target_record = raw_records.get(target_id)
    if not isinstance(raw_target_record, dict):
        raise ArtifactError("sealed remediation target has no raw failure record")
    verify_sealed_false_kill_evidence(
        root, declaration, target_id, raw_target_record, raw_chain
    )

    def sealed_rows(name: str, label: str) -> dict[str, dict[str, Any]]:
        return index_exact_rows(read_jsonl(root / name, label), ids, label)

    control_predictions = sealed_rows("control-predictions.jsonl", "control predictions")
    control_results = sealed_rows("control-results.jsonl", "control results")
    raw_predictions = sealed_rows(
        "raw-candidate-predictions.jsonl", "raw candidate predictions"
    )
    raw_results = sealed_rows("raw-candidate-results.jsonl", "raw candidate results")
    remediated_predictions = sealed_rows(
        "remediated-candidate-predictions.jsonl", "remediated predictions"
    )
    remediated_results = sealed_rows(
        "remediated-candidate-results.jsonl", "remediated results"
    )
    triage_rows = index_exact_rows(
        read_jsonl(root / "raw-n-100-triage.jsonl", "sealed raw N=100 triage"),
        ids,
        "sealed raw N=100 triage",
    )
    if (
        sealed_chain_ids != ids
        or sealed_control_predictions != control_predictions
        or sealed_candidate_predictions != raw_predictions
        or sealed_control_results != control_results
        or sealed_candidate_results != raw_results
        or sha256_file(root / "raw-n-100-triage.jsonl")
        != raw_chain[-1].get("triage_sha256")
        or {instance_id: triage_rows[instance_id].get("language") for instance_id in ids}
        != languages
        or sha256_file(root / "raw-watchdog.log")
        != raw_chain[-1].get("watchdog_sha256")
    ):
        raise ArtifactError(
            "sealed cumulative control/raw/language inputs do not reconcile to batches"
        )
    prediction_changes = [
        instance_id
        for instance_id in ids
        if raw_predictions[instance_id] != remediated_predictions[instance_id]
    ]
    result_changes = [
        instance_id
        for instance_id in ids
        if raw_results[instance_id] != remediated_results[instance_id]
    ]
    record_changes = [
        instance_id
        for instance_id in ids
        if raw_records[instance_id] != remediated_records[instance_id]
    ]
    if prediction_changes != [target_id] or result_changes != [target_id] or record_changes != [target_id]:
        raise ArtifactError("remediation changed a task outside the authorized target")
    official_prediction = index_exact_rows(
        read_jsonl(
            root / "official-evaluation/prediction.jsonl", "official target prediction"
        ),
        [target_id],
        "official target prediction",
    )[target_id]
    official_result = index_exact_rows(
        read_jsonl(root / "official-evaluation/result.jsonl", "official target result"),
        [target_id],
        "official target result",
    )[target_id]
    if (
        official_prediction != remediated_predictions[target_id]
        or official_result != remediated_results[target_id]
        or official_prediction != rerun_terminal["prediction"]
        or official_result.get("error")
    ):
        raise ArtifactError("remediation official target evaluation is invalid")

    control = synthetic_run(
        ids,
        control_predictions,
        control_results,
        languages,
        control_records,
        root / "control-watchdog.log",
    )
    raw_candidate = synthetic_run(
        ids,
        raw_predictions,
        raw_results,
        languages,
        raw_records,
        root / "raw-watchdog.log",
    )
    remediated_candidate = synthetic_run(
        ids,
        remediated_predictions,
        remediated_results,
        languages,
        remediated_records,
        root / "remediated-watchdog.log",
    )
    for field in ("failure_ids", "timeout_ids", "evaluator_error_ids"):
        raw_ids = set(raw_candidate["reliability"][field])
        repaired_ids = set(remediated_candidate["reliability"][field])
        if target_id not in raw_ids or repaired_ids != raw_ids - {target_id}:
            raise ArtifactError(
                f"remediation reliability changed IDs other than target: {field}"
            )

    origin_ids = manifest_ids(
        root / "originating-batch-manifest.json", expected_count=CHECKPOINT_SIZE
    )
    latest_ids = manifest_ids(
        root / "latest-batch-manifest.json", expected_count=CHECKPOINT_SIZE
    )
    origin_count = token.get("originating_batch", {}).get("count")
    origin_binding = next(
        (row for row in raw_chain if row.get("count") == origin_count), None
    )
    if (
        target_id not in origin_ids
        or not isinstance(origin_binding, dict)
        or origin_binding.get("batch_manifest_sha256")
        != sha256_file(root / "originating-batch-manifest.json")
        or raw_chain[-1].get("batch_manifest_sha256")
        != sha256_file(root / "latest-batch-manifest.json")
    ):
        raise ArtifactError("sealed originating/latest batch manifests are not raw-bound")

    assessment = recompute_remediation_assessment(
        ids,
        origin_ids,
        latest_ids,
        control,
        raw_candidate,
        remediated_candidate,
        root / "control-watchdog.log",
        root / "raw-watchdog.log",
        root / "remediated-watchdog.log",
    )
    expected_watchdog, removed_watchdog_events = watchdog_without_target(
        root / "raw-watchdog.log", target_id
    )
    if (root / "remediated-watchdog.log").read_bytes() != expected_watchdog:
        raise ArtifactError("sealed remediated watchdog is not the target-only overlay")
    expected_comparison = {
        "schema_version": SCHEMA_VERSION,
        "mode": "false-kill-remediation-comparison",
        "target_ids": target_ids,
        "validation": remediation_validation_summary(
            removed_watchdog_events,
            evaluator_counts["total_official_evaluator_invocations"],
        ),
        "comparison": assessment["comparison"],
        "per_language": assessment["per_language"],
        "reliability": assessment["reliability"],
        "raw_gate": assessment["raw_gate"],
        "gate": assessment["gate"],
        "per_task_raw_vs_remediated": per_task_output(
            ids, raw_candidate, remediated_candidate
        ),
    }
    comparison = read_json(root / "comparison.json", "remediation comparison")
    if comparison != expected_comparison:
        raise ArtifactError(
            "producer comparison differs from standalone sealed-input recomputation"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "verify-remediation",
        "bundle": str(root),
        "target_id": target_id,
        "sealed_files": len(sealed_files),
        "packaging_retry_recovery": recovery_mode,
        "evaluator_counts": evaluator_counts,
        "remediated_gate_passed": assessment["gate"]["passed"],
        "release_eligible": assessment["gate"]["release_eligible"],
        "independently_recomputed": {
            "raw_false_kill_evidence": True,
            "recorded_run_parity": True,
            "control_vs_remediated_metrics": True,
            "languages": True,
            "reliability": True,
            "raw_n_100_release_gate": True,
            "repaired_originating_batch_10": True,
            "latest_batch_10": True,
            "cumulative_release_gate": True,
            "per_task_raw_vs_remediated": True,
        },
        "gate": assessment["gate"],
        "remediation_sha256": sha256_file(root / "remediation.json"),
    }


def packaging_retry_paths(attempt_root: Path) -> dict[str, Path]:
    return {
        "attestation": attempt_root / "packaging-retry-attestation.json",
        "consumption": attempt_root / "packaging-retry-consumed.json",
        "evaluation": attempt_root / "packaging-retry-evaluation",
    }


@contextmanager
def exclusive_remediation_attempt(authorization_path: Path) -> Iterable[None]:
    """Serialize packaging state transitions without adding an attempt artifact."""
    attempt_root = authorization_path.parent
    if attempt_root.is_symlink() or not attempt_root.is_dir():
        raise ArtifactError("canonical remediation attempt root is invalid")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(attempt_root, flags)
    except OSError as error:
        raise ArtifactError("cannot lock canonical remediation attempt root") from error
    try:
        opened = os.fstat(descriptor)
        current = os.stat(attempt_root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
        ):
            raise ArtifactError("canonical remediation attempt root changed during lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = os.stat(attempt_root, follow_symlinks=False)
        if opened.st_dev != current.st_dev or opened.st_ino != current.st_ino:
            raise ArtifactError("canonical remediation attempt root changed during lock")
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def frozen_control_triage_bytes(state: dict[str, Any]) -> bytes:
    raw_triage_path = state["latest"]["dir"] / "control-triage.jsonl"
    raw_triage_sha256 = state["checkpoint_bindings"][-1].get("triage_sha256")
    if (
        raw_triage_path.is_symlink()
        or not raw_triage_path.is_file()
        or not isinstance(raw_triage_sha256, str)
        or sha256_file(raw_triage_path) != raw_triage_sha256
    ):
        raise ArtifactError("raw N=100 control triage differs from its frozen binding")
    return raw_triage_path.read_bytes()


def packaging_retry_attestation_core(
    authorization: dict[str, Any],
    rerun: dict[str, Any],
    evaluator_path: Path,
    runtime_manifest_path: Path,
    failed_packager_sha256: str,
    recovery_packager_sha256: str,
    correct_frozen_triage_sha256: str,
) -> dict[str, Any]:
    attempt_root = authorization["attempt_root"]
    paths = packaging_retry_paths(attempt_root)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": PACKAGING_RETRY_ATTESTATION_MODE,
        "incident_mode": PACKAGING_RETRY_INCIDENT_MODE,
        "target_id": authorization["token"]["target_id"],
        "canonical_attestation_path": str(paths["attestation"]),
        "authorization_sha256": authorization["authorization"][
            "authorization_sha256"
        ],
        "authorization_file_sha256": sha256_file(
            authorization["authorization_path"]
        ),
        "attempt_ledger_sha256": sha256_file(authorization["ledger_path"]),
        "attempt_ledger_entries": 2,
        "rerun_tree_sha256": rerun["tree_sha256"],
        "rerun_prediction_sha256": rerun["terminal"]["prediction_sha256"],
        "evaluator_sha256": sha256_file(evaluator_path),
        "runtime_manifest_sha256": sha256_file(runtime_manifest_path),
        "failed_packager_sha256": failed_packager_sha256,
        "recovery_packager_sha256": recovery_packager_sha256,
        "operator_attestation": {
            "prior_official_evaluator_succeeded": True,
            "prior_receipt_or_result_survived": False,
            "failure_stage": "post-evaluation-pre-bundle",
            "missing_path": "triage/triage.jsonl",
            "correct_frozen_path": "control-triage.jsonl",
            "correct_frozen_sha256": correct_frozen_triage_sha256,
            "bundle_absent_at_predeclaration": True,
        },
        "known_counts_before_retry": {
            "official_evaluator_invocations": 1,
            "prior_unsealed_operator_attested": 1,
            "sealed_receipts": 0,
            "aws_execution_attempts": 1,
        },
    }


def validate_packaging_retry_attestation(
    path: Path,
    authorization: dict[str, Any],
    rerun: dict[str, Any],
    evaluator_path: Path,
    runtime_manifest_path: Path,
    correct_frozen_triage_sha256: str,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ArtifactError("packaging retry attestation is missing or symlinked")
    attestation = read_json(path, "packaging retry attestation")
    if not isinstance(attestation, dict):
        raise ArtifactError("packaging retry attestation must be an object")
    _validate_hashed_object(
        attestation, "attestation_sha256", "packaging retry attestation"
    )
    expected = packaging_retry_attestation_core(
        authorization,
        rerun,
        evaluator_path,
        runtime_manifest_path,
        PACKAGING_RETRY_FAILED_PACKAGER_SHA256,
        sha256_file(Path(__file__).resolve(strict=True)),
        correct_frozen_triage_sha256,
    )
    if _drop_keys(attestation, {"attestation_sha256"}) != expected:
        raise ArtifactError("packaging retry attestation binding is invalid")
    return attestation


def predeclare_packaging_retry(
    control_root: Path,
    checkpoint_root: Path,
    candidate_root: Path,
    declaration_path: Path,
    authorization_path: Path,
    rerun_root: Path,
    evaluator_path: Path,
    runtime_manifest_path: Path,
    failed_packager_sha256: str,
) -> dict[str, Any]:
    with exclusive_remediation_attempt(authorization_path):
        return _predeclare_packaging_retry_locked(
            control_root,
            checkpoint_root,
            candidate_root,
            declaration_path,
            authorization_path,
            rerun_root,
            evaluator_path,
            runtime_manifest_path,
            failed_packager_sha256,
        )


def _predeclare_packaging_retry_locked(
    control_root: Path,
    checkpoint_root: Path,
    candidate_root: Path,
    declaration_path: Path,
    authorization_path: Path,
    rerun_root: Path,
    evaluator_path: Path,
    runtime_manifest_path: Path,
    failed_packager_sha256: str,
) -> dict[str, Any]:
    if failed_packager_sha256 != PACKAGING_RETRY_FAILED_PACKAGER_SHA256:
        raise ArtifactError("failed packager SHA256 is not the attested triage-path build")
    declaration = read_json(declaration_path, "false-kill declaration")
    target_id = verify_false_kill_declaration(
        declaration, control_root, checkpoint_root
    )
    state = load_n_100_remediation_state(
        control_root, checkpoint_root, candidate_root
    )
    _validate_prepared_remediation_token(
        authorization_path.parent / "authorization-token.json",
        checkpoint_root,
        declaration_path,
        declaration,
        state,
    )
    authorization = load_remediation_execution_authorization(
        authorization_path, state["latest"]["dir"], target_id
    )
    if rerun_root.resolve(strict=True) != authorization["rerun_root"]:
        raise ArtifactError("--rerun is not the canonical authorized rerun path")
    if len(authorization["ledger"]) != 2:
        raise ArtifactError("packaging retry requires the existing observed rerun ledger")
    if authorization["bundle_root"].exists() or authorization["bundle_root"].is_symlink():
        raise ArtifactError("packaging retry may not be predeclared after a bundle exists")
    validate_canonical_attempt_layout(authorization, bundle_may_exist=False)
    rerun = validate_remediation_rerun(
        state["latest"]["dir"], rerun_root, target_id, authorization_path
    )
    validate_runtime_manifest(runtime_manifest_path, evaluator_path)
    if (
        sha256_file(runtime_manifest_path) != state["runtime_manifest_sha256"]
        or sha256_file(evaluator_path) != state["evaluator_sha256"]
    ):
        raise ArtifactError("packaging retry evaluator/runtime differs from raw N=100")
    correct_frozen_triage_sha256 = hashlib.sha256(
        frozen_control_triage_bytes(state)
    ).hexdigest()
    paths = packaging_retry_paths(authorization["attempt_root"])
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        raise ArtifactError("packaging retry evidence was already predeclared or consumed")
    recovery_packager_sha256 = sha256_file(Path(__file__).resolve(strict=True))
    core = packaging_retry_attestation_core(
        authorization,
        rerun,
        evaluator_path,
        runtime_manifest_path,
        failed_packager_sha256,
        recovery_packager_sha256,
        correct_frozen_triage_sha256,
    )
    attestation = {
        **core,
        "attestation_sha256": canonical_sha256(core),
    }
    try:
        write_exclusive(paths["attestation"], json_bytes(attestation))
    except FileExistsError as error:
        raise ArtifactError("packaging retry attestation already exists") from error
    validate_canonical_attempt_layout(authorization, bundle_may_exist=False)
    validate_packaging_retry_attestation(
        paths["attestation"],
        authorization,
        rerun,
        evaluator_path,
        runtime_manifest_path,
        correct_frozen_triage_sha256,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "predeclared-packaging-retry",
        "target_id": target_id,
        "attestation": str(paths["attestation"]),
        "attestation_file_sha256": sha256_file(paths["attestation"]),
        "failed_packager_sha256": failed_packager_sha256,
        "recovery_packager_sha256": recovery_packager_sha256,
        "execution_ledger_entries": 2,
        "known_counts_before_retry": {
            "official_evaluator_invocations": 1,
            "prior_unsealed_operator_attested": 1,
            "sealed_receipts": 0,
            "aws_execution_attempts": 1,
        },
    }


def packaging_retry_consumption_core(
    authorization: dict[str, Any],
    rerun: dict[str, Any],
    attestation_path: Path,
) -> dict[str, Any]:
    attestation = read_json(attestation_path, "packaging retry attestation")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": PACKAGING_RETRY_CONSUMPTION_MODE,
        "target_id": authorization["token"]["target_id"],
        "retry_evaluator_invocation_number": 2,
        "attestation_file_sha256": sha256_file(attestation_path),
        "attestation_sha256": attestation["attestation_sha256"],
        "authorization_sha256": authorization["authorization"][
            "authorization_sha256"
        ],
        "attempt_ledger_sha256": sha256_file(authorization["ledger_path"]),
        "rerun_tree_sha256": rerun["tree_sha256"],
        "evaluator_sha256": attestation["evaluator_sha256"],
        "runtime_manifest_sha256": attestation["runtime_manifest_sha256"],
        "recovery_packager_sha256": attestation["recovery_packager_sha256"],
        "invocation_accounting_after_consumption": {
            "observed_prior_official_evaluator_invocations": 1,
            "retry_invocation_number_reserved": 2,
            "total_invocation_slots_consumed": 2,
            "maximum_total_official_evaluator_invocations": 2,
            "prior_unsealed_operator_attested": 1,
            "sealed_receipts": 0,
            "aws_execution_attempts": 1,
        },
    }


def validate_packaging_retry_consumption(
    path: Path,
    authorization: dict[str, Any],
    rerun: dict[str, Any],
    attestation_path: Path,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ArtifactError("packaging retry consumption is missing or symlinked")
    consumption = read_json(path, "packaging retry consumption")
    if not isinstance(consumption, dict):
        raise ArtifactError("packaging retry consumption must be an object")
    _validate_hashed_object(
        consumption, "consumption_sha256", "packaging retry consumption"
    )
    expected = packaging_retry_consumption_core(
        authorization, rerun, attestation_path
    )
    if _drop_keys(consumption, {"consumption_sha256"}) != expected:
        raise ArtifactError("packaging retry consumption binding is invalid")
    return consumption


def packaging_retry_evaluation_receipt_core(
    authorization: dict[str, Any],
    rerun: dict[str, Any],
    attestation_path: Path,
    consumption_path: Path,
    execution_receipt: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": PACKAGING_RETRY_RECEIPT_MODE,
        "target_id": authorization["token"]["target_id"],
        "attestation_file_sha256": sha256_file(attestation_path),
        "consumption_file_sha256": sha256_file(consumption_path),
        "authorization_sha256": authorization["authorization"][
            "authorization_sha256"
        ],
        "rerun_tree_sha256": rerun["tree_sha256"],
        "execution_receipt": execution_receipt,
    }


def validate_packaging_retry_evaluation(
    evaluation_path: Path,
    authorization: dict[str, Any],
    rerun: dict[str, Any],
    attestation_path: Path,
    consumption_path: Path,
) -> dict[str, Any]:
    if evaluation_path.is_symlink() or not evaluation_path.is_dir():
        raise ArtifactError("consumed packaging retry has no complete evaluation")
    actual: set[str] = set()
    for path in evaluation_path.rglob("*"):
        if path.is_symlink():
            raise ArtifactError("packaging retry evaluation may not contain symlinks")
        if path.is_file():
            actual.add(path.relative_to(evaluation_path).as_posix())
    if actual != PACKAGING_RETRY_EVALUATION_FILES:
        raise ArtifactError("packaging retry evaluation is partial or has extra files")
    wrapper = read_json(
        evaluation_path / "receipt.json", "packaging retry evaluation receipt"
    )
    if not isinstance(wrapper, dict):
        raise ArtifactError("packaging retry evaluation receipt must be an object")
    _validate_hashed_object(
        wrapper, "receipt_sha256", "packaging retry evaluation receipt"
    )
    receipt = wrapper.get("execution_receipt")
    if not isinstance(receipt, dict):
        raise ArtifactError("packaging retry evaluation has no execution receipt")
    expected = packaging_retry_evaluation_receipt_core(
        authorization,
        rerun,
        attestation_path,
        consumption_path,
        receipt,
    )
    if _drop_keys(wrapper, {"receipt_sha256"}) != expected:
        raise ArtifactError("packaging retry evaluation receipt binding is invalid")
    expected_hashes = {
        "prediction_sha256": sha256_file(evaluation_path / "prediction.jsonl"),
        "result_sha256": sha256_file(evaluation_path / "result.jsonl"),
        "stdout_sha256": sha256_file(evaluation_path / "stdout.log"),
        "stderr_sha256": sha256_file(evaluation_path / "stderr.log"),
    }
    if receipt.get("returncode") != 0 or any(
        receipt.get(key) != value for key, value in expected_hashes.items()
    ):
        raise ArtifactError("packaging retry evaluator receipt hashes are invalid")
    target_id = authorization["token"]["target_id"]
    prediction = index_exact_rows(
        read_jsonl(evaluation_path / "prediction.jsonl", "retry prediction"),
        [target_id],
        "retry prediction",
    )[target_id]
    result_row = index_exact_rows(
        read_jsonl(evaluation_path / "result.jsonl", "retry result"),
        [target_id],
        "retry result",
    )[target_id]
    if prediction != rerun["prediction"] or result_row.get("error"):
        raise ArtifactError("packaging retry evaluation target output is invalid")
    task_metrics(target_id, prediction, result_row)
    return {
        "prediction": prediction,
        "result": result_row,
        "execution_receipt": receipt,
        "receipt_wrapper": wrapper,
    }


def run_or_resume_packaging_retry_evaluation(
    authorization: dict[str, Any],
    rerun: dict[str, Any],
    runtime: dict[str, Any],
    evaluator_path: Path,
    runtime_manifest_path: Path,
    cache_dir: Path,
    correct_frozen_triage_sha256: str,
) -> dict[str, Any] | None:
    paths = packaging_retry_paths(authorization["attempt_root"])
    attestation_path = paths["attestation"]
    if not attestation_path.exists() and not attestation_path.is_symlink():
        if paths["consumption"].exists() or paths["evaluation"].exists():
            raise ArtifactError("packaging retry evidence exists without an attestation")
        return None
    validate_packaging_retry_attestation(
        attestation_path,
        authorization,
        rerun,
        evaluator_path,
        runtime_manifest_path,
        correct_frozen_triage_sha256,
    )
    if paths["consumption"].exists() or paths["consumption"].is_symlink():
        validate_packaging_retry_consumption(
            paths["consumption"], authorization, rerun, attestation_path
        )
        if not paths["evaluation"].is_dir() or paths["evaluation"].is_symlink():
            raise ArtifactError(
                "packaging retry was consumed without a complete persisted evaluation"
            )
        return validate_packaging_retry_evaluation(
            paths["evaluation"],
            authorization,
            rerun,
            attestation_path,
            paths["consumption"],
        )
    if paths["evaluation"].exists() or paths["evaluation"].is_symlink():
        raise ArtifactError("packaging retry evaluation exists without consumption")

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{authorization['attempt_root'].name}-packaging-retry-evaluation-",
            dir=authorization["attempt_root"].parent,
        )
    )
    moved = False
    try:
        prediction_path = staging / "prediction.jsonl"
        result_path = staging / "result.jsonl"
        stdout_path = staging / "stdout.log"
        stderr_path = staging / "stderr.log"
        write_bytes(prediction_path, jsonl_bytes([rerun["prediction"]]))
        environment = dict(os.environ)
        environment.update(evaluator_execution_environment(runtime))
        command = [
            str(runtime["python"]["executable"]),
            "-m",
            "contextbench.evaluate",
            "--gold",
            str(runtime["gold_parquet"]["path"]),
            "--pred",
            str(prediction_path),
            "--cache",
            str(cache_dir),
            "--out",
            str(result_path),
        ]
        with tempfile.TemporaryDirectory(
            prefix="contextbench-remediation-retry-worktrees-", dir=staging
        ) as worktree_directory:
            environment["CONTEXTBENCH_TMP_ROOT"] = worktree_directory
            consumption_core = packaging_retry_consumption_core(
                authorization, rerun, attestation_path
            )
            consumption = {
                **consumption_core,
                "consumption_sha256": canonical_sha256(consumption_core),
            }
            try:
                write_exclusive(paths["consumption"], json_bytes(consumption))
            except FileExistsError as error:
                raise ArtifactError(
                    "packaging retry evaluator was already consumed"
                ) from error
            receipt = run_evaluator_command(
                command,
                cwd=Path(runtime["contextbench_checkout"]["path"]),
                environment=environment,
                prediction_path=prediction_path,
                result_path=result_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        target_id = authorization["token"]["target_id"]
        target_result = index_exact_rows(
            read_jsonl(result_path, "packaging retry official result"),
            [target_id],
            "packaging retry official result",
        )[target_id]
        if target_result.get("error"):
            raise ArtifactError("packaging retry official evaluator did not score the target")
        task_metrics(target_id, rerun["prediction"], target_result)
        wrapper_core = packaging_retry_evaluation_receipt_core(
            authorization,
            rerun,
            attestation_path,
            paths["consumption"],
            receipt,
        )
        write_bytes(
            staging / "receipt.json",
            json_bytes(
                {
                    **wrapper_core,
                    "receipt_sha256": canonical_sha256(wrapper_core),
                }
            ),
        )
        for name in PACKAGING_RETRY_EVALUATION_FILES:
            fsync_file(staging / name)
        fsync_directory(staging)
        if paths["evaluation"].exists() or paths["evaluation"].is_symlink():
            raise ArtifactError("packaging retry evaluation destination already exists")
        source_parent = staging.parent
        os.rename(staging, paths["evaluation"])
        fsync_directory(paths["evaluation"])
        fsync_directory(paths["evaluation"].parent)
        fsync_directory(source_parent)
        moved = True
    finally:
        if not moved:
            shutil.rmtree(staging, ignore_errors=True)
    return validate_packaging_retry_evaluation(
        paths["evaluation"],
        authorization,
        rerun,
        attestation_path,
        paths["consumption"],
    )


def create_remediation_bundle(
    control_root: Path,
    checkpoint_root: Path,
    candidate_root: Path,
    declaration_path: Path,
    authorization_path: Path,
    rerun_root: Path,
    evaluator_path: Path,
    runtime_manifest_path: Path,
    cache_dir: Path,
    bundle_root: Path,
) -> dict[str, Any]:
    with exclusive_remediation_attempt(authorization_path):
        return _create_remediation_bundle_locked(
            control_root,
            checkpoint_root,
            candidate_root,
            declaration_path,
            authorization_path,
            rerun_root,
            evaluator_path,
            runtime_manifest_path,
            cache_dir,
            bundle_root,
        )


def _create_remediation_bundle_locked(
    control_root: Path,
    checkpoint_root: Path,
    candidate_root: Path,
    declaration_path: Path,
    authorization_path: Path,
    rerun_root: Path,
    evaluator_path: Path,
    runtime_manifest_path: Path,
    cache_dir: Path,
    bundle_root: Path,
) -> dict[str, Any]:
    declaration = read_json(declaration_path, "false-kill declaration")
    target_id = verify_false_kill_declaration(
        declaration, control_root, checkpoint_root
    )
    state = load_n_100_remediation_state(
        control_root, checkpoint_root, candidate_root
    )
    _validate_prepared_remediation_token(
        authorization_path.parent / "authorization-token.json",
        checkpoint_root,
        declaration_path,
        declaration,
        state,
    )
    execution_authorization = load_remediation_execution_authorization(
        authorization_path, state["latest"]["dir"], target_id
    )
    if rerun_root.resolve(strict=True) != execution_authorization["rerun_root"]:
        raise ArtifactError("--rerun is not the canonical authorized rerun path")
    if bundle_root.resolve(strict=False) != execution_authorization["bundle_root"]:
        raise ArtifactError("--bundle-root is not the canonical exclusive destination")
    if bundle_root.exists() or bundle_root.is_symlink():
        raise ArtifactError(
            f"refusing a second remediation attempt or bundle replacement: {bundle_root}"
        )
    protected_sources = (
        ("control source", control_root),
        ("checkpoint source", checkpoint_root),
        ("candidate source", candidate_root),
        ("rerun source", rerun_root),
    )
    reject_path_overlap(bundle_root, "remediation bundle", protected_sources)
    reject_path_overlap(cache_dir, "remediation evaluator cache", protected_sources)
    reject_path_overlap(
        cache_dir,
        "remediation evaluator cache",
        (("canonical attempt", execution_authorization["attempt_root"]),),
    )
    validate_canonical_attempt_layout(
        execution_authorization, bundle_may_exist=False
    )
    raw_record = state["raw_candidate"]["records"].get(target_id)
    declared_record = declaration["false_kill_attestation"]["raw_failure_record"]
    if (
        not isinstance(raw_record, dict)
        or raw_record.get("status") != "failure"
        or any(
            raw_record.get(field) != declared_record.get(field)
            for field in TERMINAL_IDENTITY_FIELDS
        )
    ):
        raise ArtifactError(
            "predeclared raw failure identity changed before the N=100 remediation"
        )
    rerun = validate_remediation_rerun(
        state["latest"]["dir"], rerun_root, target_id, authorization_path
    )
    runtime = validate_runtime_manifest(runtime_manifest_path, evaluator_path)
    if sha256_file(runtime_manifest_path) != state["runtime_manifest_sha256"]:
        raise ArtifactError("remediation evaluator runtime differs from raw N=100")
    if sha256_file(evaluator_path) != state["evaluator_sha256"]:
        raise ArtifactError("remediation evaluator source differs from raw N=100")
    runtime_checkout = Path(runtime["contextbench_checkout"]["path"]).resolve(
        strict=True
    )
    reject_path_overlap(
        cache_dir,
        "remediation evaluator cache",
        (
            ("pinned ContextBench checkout", runtime_checkout),
            ("pinned evaluator", evaluator_path),
            ("pinned runtime manifest", runtime_manifest_path),
        ),
    )
    if cache_dir.is_symlink():
        raise ArtifactError("remediation evaluator cache may not be a symlink")
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not cache_dir.is_dir():
        raise ArtifactError("remediation evaluator cache is not a directory")
    raw_triage_bytes = frozen_control_triage_bytes(state)
    correct_frozen_triage_sha256 = hashlib.sha256(raw_triage_bytes).hexdigest()
    rerun_was_already_observed = len(execution_authorization["ledger"]) == 2
    bind_rerun_observation_to_ledger(rerun)
    recovery_evaluation = run_or_resume_packaging_retry_evaluation(
        execution_authorization,
        rerun,
        runtime,
        evaluator_path,
        runtime_manifest_path,
        cache_dir,
        correct_frozen_triage_sha256,
    )
    if recovery_evaluation is None and rerun_was_already_observed:
        raise ArtifactError(
            "an observed rerun without a bundle requires a predeclared packaging retry"
        )
    recovery_mode = recovery_evaluation is not None
    evaluator_counts = {
        "total_official_evaluator_invocations": 2 if recovery_mode else 1,
        "prior_unsealed_operator_attested": 1 if recovery_mode else 0,
        "sealed_receipts": 1,
        "aws_execution_attempts": 1,
    }

    bundle_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{bundle_root.name}-", dir=bundle_root.parent)
    )
    moved = False
    try:
        official_dir = temporary / "official-evaluation"
        official_dir.mkdir()
        official_prediction_path = official_dir / "prediction.jsonl"
        official_result_path = official_dir / "result.jsonl"
        stdout_path = official_dir / "stdout.log"
        stderr_path = official_dir / "stderr.log"
        if recovery_evaluation is None:
            write_bytes(
                official_prediction_path,
                jsonl_bytes([rerun["prediction"]]),
            )
            environment = dict(os.environ)
            environment.update(evaluator_execution_environment(runtime))
            with tempfile.TemporaryDirectory(
                prefix="contextbench-remediation-worktrees-", dir=temporary
            ) as worktree_directory:
                environment["CONTEXTBENCH_TMP_ROOT"] = worktree_directory
                command = [
                    str(runtime["python"]["executable"]),
                    "-m",
                    "contextbench.evaluate",
                    "--gold",
                    str(runtime["gold_parquet"]["path"]),
                    "--pred",
                    str(official_prediction_path),
                    "--cache",
                    str(cache_dir),
                    "--out",
                    str(official_result_path),
                ]
                receipt = run_evaluator_command(
                    command,
                    cwd=Path(runtime["contextbench_checkout"]["path"]),
                    environment=environment,
                    prediction_path=official_prediction_path,
                    result_path=official_result_path,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )
            target_result = index_exact_rows(
                read_jsonl(official_result_path, "remediation official result"),
                [target_id],
                "remediation official result",
            )[target_id]
            if target_result.get("error"):
                raise ArtifactError(
                    "remediation official evaluator did not score the target"
                )
            task_metrics(target_id, rerun["prediction"], target_result)
        else:
            retry_paths = packaging_retry_paths(
                execution_authorization["attempt_root"]
            )
            for name in ("prediction.jsonl", "result.jsonl", "stdout.log", "stderr.log"):
                write_bytes(
                    official_dir / name,
                    (retry_paths["evaluation"] / name).read_bytes(),
                )
            receipt = recovery_evaluation["execution_receipt"]
            target_result = recovery_evaluation["result"]
            write_bytes(
                temporary / "packaging-retry-attestation.json",
                retry_paths["attestation"].read_bytes(),
            )
            write_bytes(
                temporary / "packaging-retry-consumed.json",
                retry_paths["consumption"].read_bytes(),
            )
            write_bytes(
                temporary / "packaging-retry-evaluation-receipt.json",
                (retry_paths["evaluation"] / "receipt.json").read_bytes(),
            )
            write_bytes(
                temporary / "packaging-retry-packager.py",
                Path(__file__).resolve(strict=True).read_bytes(),
            )

        ids = state["ids"]
        raw_candidate = state["raw_candidate"]
        remediated_predictions = copy.deepcopy(raw_candidate["predictions"])
        remediated_results = copy.deepcopy(raw_candidate["results"])
        remediated_records = copy.deepcopy(raw_candidate["records"])
        remediated_predictions[target_id] = copy.deepcopy(rerun["prediction"])
        remediated_results[target_id] = copy.deepcopy(target_result)
        terminal = rerun["terminal"]
        remediated_records[target_id] = {
            "status": "success",
            "seconds": terminal.get("seconds"),
            "failure_kind": None,
            "run_record_sha256": terminal.get("run_record_sha256"),
            "prediction_sha256": terminal.get("prediction_sha256"),
            "audit_sha256": terminal.get("audit_sha256"),
            "query_plan_sha256": terminal.get("query_plan_sha256"),
            "failure_sha256": None,
            "validation_mode": "hash_bound_terminal_v1",
            "remediation_source": "rerun/run_record.json",
        }
        watchdog_bytes, removed_watchdog_events = watchdog_without_target(
            state["latest"]["dir"] / "watchdog.log", target_id
        )
        remediated_watchdog = temporary / "remediated-watchdog.log"
        write_bytes(remediated_watchdog, watchdog_bytes)
        remediated_candidate = synthetic_run(
            ids,
            remediated_predictions,
            remediated_results,
            state["languages"],
            remediated_records,
            remediated_watchdog,
        )
        for instance_id in ids:
            if instance_id == target_id:
                continue
            if (
                remediated_predictions[instance_id]
                != raw_candidate["predictions"][instance_id]
                or remediated_results[instance_id]
                != raw_candidate["results"][instance_id]
                or remediated_records[instance_id]
                != raw_candidate["records"][instance_id]
                or remediated_candidate["per_task"][instance_id]
                != raw_candidate["per_task"][instance_id]
            ):
                raise ArtifactError(
                    f"remediation changed a non-target task: {instance_id}"
                )
        for field in ("failure_ids", "timeout_ids", "evaluator_error_ids"):
            raw_ids = set(raw_candidate["reliability"][field])
            repaired_ids = set(remediated_candidate["reliability"][field])
            if target_id not in raw_ids or repaired_ids != raw_ids - {target_id}:
                raise ArtifactError(
                    f"remediation reliability differs outside the target: {field}"
                )

        origin = originating_failure_checkpoint(state["chain"], target_id)
        assessment = recompute_remediation_assessment(
            ids,
            origin["batch_ids"],
            state["latest"]["batch_ids"],
            state["control"],
            raw_candidate,
            remediated_candidate,
            control_root / "watchdog.log",
            state["latest"]["dir"] / "watchdog.log",
            remediated_watchdog,
        )
        comparison = {
            "schema_version": SCHEMA_VERSION,
            "mode": "false-kill-remediation-comparison",
            "target_ids": [target_id],
            "validation": remediation_validation_summary(
                removed_watchdog_events,
                evaluator_counts["total_official_evaluator_invocations"],
            ),
            "comparison": assessment["comparison"],
            "per_language": assessment["per_language"],
            "reliability": assessment["reliability"],
            "raw_gate": assessment["raw_gate"],
            "gate": assessment["gate"],
            "per_task_raw_vs_remediated": per_task_output(
                ids, raw_candidate, remediated_candidate
            ),
        }

        write_bytes(
            temporary / "manifest.json",
            (state["latest"]["dir"] / "manifest.json").read_bytes(),
        )
        write_bytes(temporary / "declaration.json", declaration_path.read_bytes())
        write_bytes(
            temporary / "authorization-token.json",
            execution_authorization["token_path"].read_bytes(),
        )
        write_bytes(
            temporary / "authorization.json",
            execution_authorization["authorization_path"].read_bytes(),
        )
        write_bytes(
            temporary / "attempt-ledger.jsonl",
            execution_authorization["ledger_path"].read_bytes(),
        )
        write_bytes(
            temporary / "originating-batch-manifest.json",
            (origin["dir"] / "batch-manifest.json").read_bytes(),
        )
        write_bytes(
            temporary / "latest-batch-manifest.json",
            (state["latest"]["dir"] / "batch-manifest.json").read_bytes(),
        )
        write_bytes(
            temporary / "raw-checkpoint-chain.json",
            json_bytes(state["checkpoint_bindings"]),
        )
        for checkpoint in state["chain"]:
            count = len(checkpoint["ids"])
            source = checkpoint["dir"]
            sealed_batch = temporary / "sealed-batches" / f"checkpoint-{count:04d}"
            write_bytes(
                sealed_batch / "batch-manifest.json",
                (source / "batch-manifest.json").read_bytes(),
            )
            write_bytes(
                sealed_batch / "control-predictions.jsonl",
                (source / "control-batch-predictions.jsonl").read_bytes(),
            )
            write_bytes(
                sealed_batch / "candidate-predictions.jsonl",
                (source / "candidate-batch-predictions.jsonl").read_bytes(),
            )
            write_bytes(
                sealed_batch / "control-results.jsonl",
                (source / "evaluation/control-results.jsonl").read_bytes(),
            )
            write_bytes(
                sealed_batch / "candidate-results.jsonl",
                (source / "evaluation/candidate-results.jsonl").read_bytes(),
            )
        write_bytes(
            temporary / "raw-n-100-triage.jsonl",
            raw_triage_bytes,
        )
        write_bytes(
            temporary / "raw-n-100-run/run_meta.json",
            (state["latest"]["dir"] / "run_meta.json").read_bytes(),
        )
        write_bytes(
            temporary / "raw-n-100-run/run_provenance.json",
            (state["latest"]["dir"] / "run_provenance.json").read_bytes(),
        )
        write_bytes(temporary / "comparison.json", json_bytes(comparison))
        write_bytes(temporary / "languages.json", json_bytes(state["languages"]))
        write_bytes(
            temporary / "control-predictions.jsonl",
            jsonl_bytes(
                state["control"]["predictions"][instance_id]
                for instance_id in ids
            ),
        )
        write_bytes(
            temporary / "control-results.jsonl",
            jsonl_bytes(
                state["control"]["results"][instance_id]
                for instance_id in ids
            ),
        )
        write_bytes(
            temporary / "control-records.json",
            json_bytes(state["control"]["records"]),
        )
        write_bytes(
            temporary / "raw-candidate-predictions.jsonl",
            jsonl_bytes(raw_candidate["predictions"][instance_id] for instance_id in ids),
        )
        write_bytes(
            temporary / "remediated-candidate-predictions.jsonl",
            jsonl_bytes(remediated_predictions[instance_id] for instance_id in ids),
        )
        write_bytes(
            temporary / "raw-candidate-results.jsonl",
            jsonl_bytes(raw_candidate["results"][instance_id] for instance_id in ids),
        )
        write_bytes(
            temporary / "remediated-candidate-results.jsonl",
            jsonl_bytes(remediated_results[instance_id] for instance_id in ids),
        )
        write_bytes(
            temporary / "raw-candidate-records.json",
            json_bytes(raw_candidate["records"]),
        )
        write_bytes(
            temporary / "remediated-candidate-records.json",
            json_bytes(remediated_records),
        )
        write_bytes(
            official_dir / "runtime-manifest.json",
            runtime_manifest_path.read_bytes(),
        )
        write_bytes(
            official_dir / "evaluate.py",
            evaluator_path.read_bytes(),
        )
        write_bytes(
            temporary / "raw-watchdog.log",
            (state["latest"]["dir"] / "watchdog.log").read_bytes(),
        )
        write_bytes(
            temporary / "control-watchdog.log",
            (control_root / "watchdog.log").read_bytes(),
        )
        for relative, source in rerun["tree_files"].items():
            write_bytes(temporary / "rerun" / relative, source.read_bytes())

        origin_completion = read_json(origin["dir"] / "completion-records.json")
        origin_record = origin_completion["per_instance"][target_id]
        for key in (
            "run_record_relative_path",
            "prediction_relative_path",
            "audit_relative_path",
            "failure_relative_path",
        ):
            relative = origin_record.get(key)
            if isinstance(relative, str):
                source = origin["dir"] / "source-artifacts" / relative
                write_bytes(temporary / "raw-target" / relative, source.read_bytes())
        raw_watchdog = _selected_watchdog_lines(
            origin["dir"] / "watchdog.log", {target_id}
        )
        write_bytes(temporary / "raw-target/watchdog.log", raw_watchdog)

        sealed_files = {
            str(path.relative_to(temporary)): sha256_file(path)
            for path in sorted(temporary.rglob("*"))
            if path.is_file()
        }
        bundle_binding = {
            "target_ids": [target_id],
            "declaration_sha256": sha256_file(temporary / "declaration.json"),
            "authorization_token_sha256": sha256_file(
                temporary / "authorization-token.json"
            ),
            "authorization_sha256": sha256_file(
                temporary / "authorization.json"
            ),
            "attempt_ledger_sha256": sha256_file(
                temporary / "attempt-ledger.jsonl"
            ),
            "raw_checkpoint_chain_sha256": sha256_file(
                temporary / "raw-checkpoint-chain.json"
            ),
            "raw_n_100_checkpoint_sha256": sha256_file(
                state["latest"]["dir"] / "checkpoint.json"
            ),
            "raw_n_100_run_meta_sha256": sha256_file(
                temporary / "raw-n-100-run/run_meta.json"
            ),
            "raw_n_100_run_provenance_sha256": sha256_file(
                temporary / "raw-n-100-run/run_provenance.json"
            ),
            "comparison_sha256": sha256_file(temporary / "comparison.json"),
            "evaluator_sha256": state["evaluator_sha256"],
            "runtime_manifest_sha256": sha256_file(
                official_dir / "runtime-manifest.json"
            ),
            "execution_receipt_sha256": canonical_sha256(receipt),
            "rerun_tree_sha256": rerun["tree_sha256"],
            "packaging_retry_attestation_sha256": (
                sha256_file(temporary / "packaging-retry-attestation.json")
                if recovery_mode
                else None
            ),
            "packaging_retry_consumption_sha256": (
                sha256_file(temporary / "packaging-retry-consumed.json")
                if recovery_mode
                else None
            ),
            "packaging_retry_evaluation_receipt_sha256": (
                sha256_file(
                    temporary / "packaging-retry-evaluation-receipt.json"
                )
                if recovery_mode
                else None
            ),
            "recovery_packager_sha256": (
                sha256_file(temporary / "packaging-retry-packager.py")
                if recovery_mode
                else None
            ),
            "sealed_files": sealed_files,
        }
        seal = {
            "schema_version": SCHEMA_VERSION,
            "mode": "false-kill-remediation-overlay",
            "target_ids": [target_id],
            "raw_history_rewritten": False,
            "source_runs_mutated": False,
            "second_attempt_or_replacement_allowed": False,
            "packaging_retry_recovery": recovery_mode,
            "parity": rerun["parity"],
            "rerun_tree": {
                "schema": "exhaustive-fresh-remediation-rerun-v1",
                "files": rerun["tree_hashes"],
                "sha256": rerun["tree_sha256"],
            },
            "evaluator": {
                "sha256": state["evaluator_sha256"],
                "runtime_manifest_sha256": state["runtime_manifest_sha256"],
                "same_unchanged_official_entrypoint": True,
                "invocations": evaluator_counts[
                    "total_official_evaluator_invocations"
                ],
                **evaluator_counts,
            },
            "evaluator_worktree_isolation": REMEDIATION_EVALUATOR_WORKTREE_ISOLATION,
            "execution_receipt": receipt,
            "bundle_binding": bundle_binding,
            "bundle_binding_sha256": canonical_sha256(bundle_binding),
            "sealed_files": sealed_files,
        }
        write_bytes(temporary / "remediation.json", json_bytes(seal))
        verify_remediation_bundle(temporary)
        os.replace(temporary, bundle_root)
        moved = True
        validate_canonical_attempt_layout(
            execution_authorization, bundle_may_exist=True
        )
        verification = verify_remediation_bundle(bundle_root)
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "remediation-overlay",
            "bundle": str(bundle_root),
            "target_id": target_id,
            "raw_history_rewritten": False,
            "source_runs_mutated": False,
            "comparison": comparison,
            "verification": verification,
        }
    except BaseException:
        if moved:
            shutil.rmtree(bundle_root, ignore_errors=True)
        else:
            shutil.rmtree(temporary, ignore_errors=True)
        raise


def diagnostic_pair_comparison(
    control_run_root: Path,
    pair_root: Path,
    evaluator_path: Path,
) -> dict[str, Any]:
    """Compare an explicitly frozen non-10 current set without making it a gate."""
    control_dir = pair_root / "control"
    candidate_dir = pair_root / "candidate"
    require_files(
        control_dir,
        ("predictions.jsonl", "results.jsonl", "leaderboard-report.json", "evaluator.log"),
    )
    require_files(
        candidate_dir,
        ("predictions.jsonl", "results.jsonl", "leaderboard-report.json", "evaluator.log"),
    )
    ids = manifest_ids(pair_root / "manifest.json")
    if len(ids) >= FULL_MANIFEST_SIZE:
        raise ArtifactError("diagnostic pair is only for a frozen pre-100 current set")
    control_full = load_run(control_run_root, expected_count=FULL_MANIFEST_SIZE)
    if not set(ids) <= set(control_full["ids"]):
        raise ArtifactError("diagnostic manifest contains IDs outside the control universe")
    parity = validate_parity(control_dir, candidate_dir)
    control_predictions = index_exact_rows(
        read_jsonl(control_dir / "predictions.jsonl", "diagnostic control predictions"),
        ids,
        "diagnostic control predictions",
    )
    candidate_predictions = index_exact_rows(
        read_jsonl(candidate_dir / "predictions.jsonl", "diagnostic candidate predictions"),
        ids,
        "diagnostic candidate predictions",
    )
    control_results = index_exact_rows(
        read_jsonl(control_dir / "results.jsonl", "diagnostic control results"),
        ids,
        "diagnostic control results",
    )
    candidate_results = index_exact_rows(
        read_jsonl(candidate_dir / "results.jsonl", "diagnostic candidate results"),
        ids,
        "diagnostic candidate results",
    )
    control_report = read_json(control_dir / "leaderboard-report.json")
    candidate_report = read_json(candidate_dir / "leaderboard-report.json")
    control_per_task = {
        instance_id: task_metrics(
            instance_id, control_predictions[instance_id], control_results[instance_id]
        )
        for instance_id in ids
    }
    candidate_per_task = {
        instance_id: task_metrics(
            instance_id, candidate_predictions[instance_id], candidate_results[instance_id]
        )
        for instance_id in ids
    }
    validate_report(
        control_report,
        ids,
        control_predictions,
        control_results,
        macro_metrics(ids, control_per_task),
    )
    validate_report(
        candidate_report,
        ids,
        candidate_predictions,
        candidate_results,
        macro_metrics(ids, candidate_per_task),
    )

    diagnostic_root = pair_root.parent
    freeze = read_json(diagnostic_root / "freeze.json", "diagnostic freeze")
    validation = read_json(diagnostic_root / "validation.json", "diagnostic validation")
    if not isinstance(freeze, dict) or not isinstance(validation, dict):
        raise ArtifactError("diagnostic freeze/validation metadata must be objects")
    tasks = freeze.get("tasks")
    if not isinstance(tasks, list) or [task.get("instance_id") for task in tasks] != ids:
        raise ArtifactError("diagnostic freeze task order differs from the paired manifest")
    ordering = [
        (int(task["terminal_mtime_ns"]), int(task["manifest_index"])) for task in tasks
    ]
    if ordering != sorted(ordering):
        raise ArtifactError("diagnostic freeze does not follow terminal mtime/manifest order")
    if validation.get("status") != "passed" or validation.get("task_ids") != ids:
        raise ArtifactError("diagnostic validation did not pass for the exact paired IDs")
    candidate_lines = [
        line + "\n"
        for line in (candidate_dir / "predictions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(candidate_lines) != len(ids):
        raise ArtifactError("diagnostic candidate prediction line count differs from manifest")
    frozen_hashes = {task["instance_id"]: task.get("prediction_sha256") for task in tasks}
    for instance_id, line in zip(ids, candidate_lines):
        if hashlib.sha256(line.encode("utf-8")).hexdigest() != frozen_hashes[instance_id]:
            raise ArtifactError(f"diagnostic candidate prediction hash changed: {instance_id}")
        if control_predictions[instance_id] != control_full["predictions"][instance_id]:
            raise ArtifactError(f"diagnostic control prediction drifted: {instance_id}")

    timing = read_json(pair_root / "timing.json", "diagnostic timing")
    if not isinstance(timing, dict):
        raise ArtifactError("diagnostic timing must be an object")
    control_records = timing.get("control")
    candidate_records = timing.get("candidate")
    if not isinstance(control_records, dict) or set(control_records) != set(ids):
        raise ArtifactError("diagnostic control timing IDs differ from manifest")
    if not isinstance(candidate_records, dict) or set(candidate_records) != set(ids):
        raise ArtifactError("diagnostic candidate timing IDs differ from manifest")
    languages = {instance_id: control_full["languages"][instance_id] for instance_id in ids}
    control = synthetic_run(
        ids, control_predictions, control_results, languages, control_records, None
    )
    candidate = synthetic_run(
        ids, candidate_predictions, candidate_results, languages, candidate_records, None
    )
    comparison = compare_metric_set(ids, control, candidate)
    language_comparison = per_language_comparison(ids, control, candidate)
    gate = provisional_gate(
        comparison,
        language_comparison,
        control["reliability"],
        candidate["reliability"],
    )
    gate["diagnostic_only"] = True
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "diagnostic-pair",
        "inputs": {
            "control_run": str(control_run_root),
            "pair": str(pair_root),
            "evaluator": str(evaluator_path),
        },
        "validation": {
            "exact_frozen_ids": True,
            "terminal_order": "run_record.json mtime_ns; original manifest order tie-break",
            "control_prediction_identity": True,
            "candidate_prediction_hashes": True,
            "current_reports_recomputed": True,
            "same_evaluator_entrypoint_for_pair": True,
            "evaluator_sha256": sha256_file(evaluator_path),
            "evaluator_environment": {"SYMBOL_DETAIL_MAX": "0"},
            "pass_at_1": None,
            "parity": parity,
        },
        "comparison": comparison,
        "per_language": language_comparison,
        "reliability": {
            "control": control["reliability"],
            "candidate": candidate["reliability"],
        },
        "wall_clock": {
            "control": control["wall_clock"],
            "candidate": candidate["wall_clock"],
        },
        "gate": gate,
        "per_task": per_task_output(ids, control, candidate),
    }


def output_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ArtifactError(f"--json-out must be an absolute path: {value}")
    if any(part.casefold() == "latest" for part in path.parts):
        raise ArtifactError("--json-out may not contain a LATEST path component")
    return path.resolve(strict=False)


def write_output(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json_bytes(payload)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def concise_text(payload: dict[str, Any]) -> str:
    mode = payload.get("mode")
    if mode == "predeclared-false-kill-remediation":
        return (
            "predeclared false-kill remediation target="
            f"{payload['target_ids'][0]} at N="
            f"{payload['raw_inputs']['originating_checkpoint_count']}"
        )
    if mode == "prepared-remediation-authorization":
        return (
            f"prepared post-N=100 remediation target={payload['target_id']} "
            "(execution not authorized)"
        )
    if mode == "authorized-remediation-execution":
        return (
            f"authorized one fresh remediation target={payload['target_id']} "
            f"run={payload['rerun_id']} resume=false"
        )
    if mode == "remediation-overlay":
        comparison = payload["comparison"]["comparison"]["control_vs_remediated"]
        status = "PASS" if payload["comparison"]["gate"]["passed"] else "FAIL"
        return (
            f"{status} remediated N=100 target={payload['target_id']} "
            f"line-F1 delta={comparison['delta']['line']['f1']:+.4f}; "
            f"raw history preserved"
        )
    if mode == "verify-remediation":
        return (
            f"verified remediation target={payload['target_id']} "
            f"sealed_files={payload['sealed_files']}"
        )
    if mode == "freeze-checkpoints":
        return (
            f"checkpoint freeze: terminal={payload['terminal_tasks']} "
            f"new={payload['newly_frozen']} pending={payload['pending_terminal_tasks']} "
            f"created={len(payload['new_checkpoints'])}"
        )
    if mode == "seal-batch":
        return (
            f"sealed checkpoint N={payload['completed_count']} batch=10 "
            f"evaluator={payload['evaluator_sha256'][:12]}"
        )
    if mode == "checkpoint":
        cumulative = payload["cumulative"]
        batch = payload["batch"]
        ci = cumulative["line_f1_bootstrap"]["ci_95"]
        status = "PASS" if payload["gate"]["passed"] else "FAIL"
        return (
            f"{status} checkpoint N={cumulative['count']} "
            f"batch10 line-F1 delta={batch['delta']['line']['f1']:+.4f}; "
            f"cumulative delta={cumulative['delta']['line']['f1']:+.4f} "
            f"95% CI=[{ci['lower']:+.4f}, {ci['upper']:+.4f}]"
        )
    if mode == "diagnostic-pair":
        comparison = payload["comparison"]
        ci = comparison["line_f1_bootstrap"]["ci_95"]
        status = "NO CONFIRMED REGRESSION" if payload["gate"]["passed"] else "REGRESSION"
        return (
            f"{status} diagnostic N={comparison['count']} "
            f"file/symbol/line F1 deltas="
            f"{comparison['delta']['file']['f1']:+.4f}/"
            f"{comparison['delta']['symbol']['f1']:+.4f}/"
            f"{comparison['delta']['line']['f1']:+.4f}; "
            f"line 95% CI=[{ci['lower']:+.4f}, {ci['upper']:+.4f}]"
        )
    if mode == "full":
        comparison = payload["comparison"]
        ci = comparison["line_f1_bootstrap"]["ci_95"]
        reliability = payload["reliability"]
        status = "PASS" if payload["gate"]["passed"] else "FAIL"
        return (
            f"{status} full N={comparison['count']} "
            f"file/symbol/line F1 deltas="
            f"{comparison['delta']['file']['f1']:+.4f}/"
            f"{comparison['delta']['symbol']['f1']:+.4f}/"
            f"{comparison['delta']['line']['f1']:+.4f}; "
            f"line 95% CI=[{ci['lower']:+.4f}, {ci['upper']:+.4f}]; "
            f"failures={reliability['control']['failures']}->"
            f"{reliability['candidate']['failures']}, timeouts="
            f"{reliability['control']['timeouts']}->"
            f"{reliability['candidate']['timeouts']}"
        )
    return json.dumps(payload, sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    compare = subparsers.add_parser("compare", help="strict paired comparison of two complete runs")
    compare.add_argument("--control", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--candidate-treatment", choices=CANDIDATE_TREATMENT_CHOICES)
    compare.add_argument("--json-out", required=True)

    freeze = subparsers.add_parser(
        "freeze-checkpoints",
        help="copy newly terminal tasks into immutable 10-task checkpoints",
    )
    freeze.add_argument("--control", required=True)
    freeze.add_argument("--candidate", required=True)
    freeze.add_argument("--checkpoint-root", required=True)
    freeze.add_argument("--candidate-treatment", choices=CANDIDATE_TREATMENT_CHOICES)
    freeze.add_argument("--json-out", required=True)

    seal = subparsers.add_parser(
        "seal-batch",
        help="run and seal the unchanged official evaluator for both sides of one batch",
    )
    seal.add_argument("--checkpoint-dir", required=True)
    seal.add_argument("--evaluator", required=True)
    seal.add_argument("--runtime-manifest", required=True)
    seal.add_argument("--cache-dir", required=True)
    seal.add_argument("--candidate-treatment", choices=CANDIDATE_TREATMENT_CHOICES)
    seal.add_argument("--json-out", required=True)

    checkpoint = subparsers.add_parser(
        "compare-checkpoint",
        help="accumulate sealed batch pairs and compare exact batch10 plus cumulative N",
    )
    checkpoint.add_argument("--control", required=True)
    checkpoint.add_argument("--checkpoint-root", required=True)
    checkpoint.add_argument("--count", type=int, required=True)
    checkpoint.add_argument(
        "--candidate-run",
        help="required at N=100; explicit complete run retaining per-task terminal artifacts",
    )
    checkpoint.add_argument(
        "--candidate-treatment", choices=CANDIDATE_TREATMENT_CHOICES
    )
    checkpoint.add_argument("--json-out", required=True)

    diagnostic = subparsers.add_parser(
        "compare-diagnostic",
        help="compare an explicitly frozen pre-10 current set without release gating",
    )
    diagnostic.add_argument("--control-run", required=True)
    diagnostic.add_argument("--pair-dir", required=True)
    diagnostic.add_argument("--evaluator", required=True)
    diagnostic.add_argument("--json-out", required=True)

    predeclare = subparsers.add_parser(
        "predeclare-remediation",
        help=(
            "bind one proven impossible-watchdog false kill before any one-task rerun"
        ),
    )
    predeclare.add_argument("--control", required=True)
    predeclare.add_argument("--checkpoint-root", required=True)
    predeclare.add_argument("--target-id", required=True)
    predeclare.add_argument("--json-out", required=True)

    prepare_remediation = subparsers.add_parser(
        "prepare-remediation-authorization",
        help="after raw N=100, create the canonical non-executable token/manifest root",
    )
    prepare_remediation.add_argument("--control", required=True)
    prepare_remediation.add_argument("--checkpoint-root", required=True)
    prepare_remediation.add_argument("--candidate-run", required=True)
    prepare_remediation.add_argument("--declaration", required=True)
    prepare_remediation.add_argument("--rerun-manifest", required=True)
    prepare_remediation.add_argument("--rerun-id", required=True)
    prepare_remediation.add_argument("--nonce", required=True)
    prepare_remediation.add_argument("--json-out", required=True)

    snapshot_provenance = subparsers.add_parser(
        "snapshot-remediation-provenance",
        help="compute the exact prospective driver provenance without executing a task",
    )
    snapshot_provenance.add_argument("--driver", required=True)
    snapshot_provenance.add_argument("--authorization-token", required=True)
    snapshot_provenance.add_argument("--raw-provenance", required=True)
    snapshot_provenance.add_argument("--dataset", required=True)
    snapshot_provenance.add_argument("--manifest", required=True)
    snapshot_provenance.add_argument("--line-budget", type=int, required=True)
    snapshot_provenance.add_argument("--selector-model")
    snapshot_provenance.add_argument("--selector-mode", required=True)
    snapshot_provenance.add_argument("--rerank-model-dir")
    snapshot_provenance.add_argument("--reinclude-tracked-dirs", action="store_true")
    snapshot_provenance.add_argument("--query-plans", action="store_true")
    snapshot_provenance.add_argument("--timeout", type=int, required=True)
    snapshot_provenance.add_argument("--chdir", required=True)
    snapshot_provenance.add_argument("--json-out", required=True)

    authorize_remediation = subparsers.add_parser(
        "authorize-remediation",
        help="write the exclusive post-N=100 execution authorization and attempt ledger",
    )
    authorize_remediation.add_argument("--control", required=True)
    authorize_remediation.add_argument("--checkpoint-root", required=True)
    authorize_remediation.add_argument("--candidate-run", required=True)
    authorize_remediation.add_argument("--declaration", required=True)
    authorize_remediation.add_argument("--authorization-token", required=True)
    authorize_remediation.add_argument("--prospective-provenance", required=True)
    authorize_remediation.add_argument("--json-out", required=True)

    predeclare_packaging = subparsers.add_parser(
        "predeclare-packaging-retry",
        help=(
            "predeclare the one recovery evaluator invocation after the attested "
            "post-evaluation triage packaging failure"
        ),
    )
    predeclare_packaging.add_argument("--control", required=True)
    predeclare_packaging.add_argument("--checkpoint-root", required=True)
    predeclare_packaging.add_argument("--candidate-run", required=True)
    predeclare_packaging.add_argument("--declaration", required=True)
    predeclare_packaging.add_argument("--authorization", required=True)
    predeclare_packaging.add_argument("--rerun", required=True)
    predeclare_packaging.add_argument("--evaluator", required=True)
    predeclare_packaging.add_argument("--runtime-manifest", required=True)
    predeclare_packaging.add_argument("--failed-packager-sha256", required=True)
    predeclare_packaging.add_argument("--json-out", required=True)

    remediate = subparsers.add_parser(
        "remediate-false-kill",
        help=(
            "after raw N=100, evaluate one predeclared same-product rerun into a new "
            "immutable overlay"
        ),
    )
    remediate.add_argument("--control", required=True)
    remediate.add_argument("--checkpoint-root", required=True)
    remediate.add_argument("--candidate-run", required=True)
    remediate.add_argument("--declaration", required=True)
    remediate.add_argument("--authorization", required=True)
    remediate.add_argument("--rerun", required=True)
    remediate.add_argument("--evaluator", required=True)
    remediate.add_argument("--runtime-manifest", required=True)
    remediate.add_argument("--cache-dir", required=True)
    remediate.add_argument("--bundle-root", required=True)
    remediate.add_argument("--json-out", required=True)

    verify_remediation = subparsers.add_parser(
        "verify-remediation",
        help="verify every hash and one-target invariant in an immutable remediation bundle",
    )
    verify_remediation.add_argument("--bundle", required=True)
    verify_remediation.add_argument("--json-out", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    json_out: Path | None = None
    try:
        json_out = output_path(args.json_out)
        if args.command == "compare":
            control = explicit_path(args.control, "control")
            candidate = explicit_path(args.candidate, "candidate")
            if json_out == control or control in json_out.parents:
                raise ArtifactError("--json-out may not write into the control run")
            if json_out == candidate or candidate in json_out.parents:
                raise ArtifactError("--json-out may not write into the candidate run")
            payload = full_comparison(control, candidate, args.candidate_treatment)
            exit_code = 0 if payload["gate"]["passed"] else 1
        elif args.command == "freeze-checkpoints":
            control = explicit_path(args.control, "control")
            candidate = explicit_path(args.candidate, "candidate")
            checkpoint_root = explicit_path(
                args.checkpoint_root, "checkpoint root", must_exist=False
            )
            if control in json_out.parents or candidate in json_out.parents:
                raise ArtifactError("--json-out may not write into a source run")
            payload = freeze_checkpoints(
                control,
                candidate,
                checkpoint_root,
                args.candidate_treatment,
            )
            exit_code = 0
        elif args.command == "seal-batch":
            checkpoint_dir = explicit_path(args.checkpoint_dir, "checkpoint directory")
            payload = evaluate_and_seal_batch(
                checkpoint_dir,
                explicit_file(args.evaluator, "evaluator"),
                explicit_file(args.runtime_manifest, "runtime manifest"),
                explicit_path(args.cache_dir, "evaluator cache", must_exist=False),
                args.candidate_treatment,
            )
            exit_code = 0
        elif args.command == "compare-checkpoint":
            control = explicit_path(args.control, "control")
            checkpoint_root = explicit_path(args.checkpoint_root, "checkpoint root")
            candidate_run = (
                explicit_path(args.candidate_run, "candidate run")
                if args.candidate_run
                else None
            )
            payload = checkpoint_comparison(
                control,
                checkpoint_root,
                args.count,
                candidate_run,
                args.candidate_treatment,
            )
            exit_code = 0 if payload["gate"]["passed"] else 1
        elif args.command == "compare-diagnostic":
            control_run = explicit_path(args.control_run, "control run")
            pair = explicit_path(args.pair_dir, "diagnostic pair")
            evaluator = explicit_file(args.evaluator, "evaluator")
            if control_run in json_out.parents or pair in json_out.parents:
                raise ArtifactError("--json-out may not write into diagnostic source artifacts")
            payload = diagnostic_pair_comparison(control_run, pair, evaluator)
            exit_code = 0 if payload["gate"]["passed"] else 1
        elif args.command == "predeclare-remediation":
            control = explicit_path(args.control, "control")
            checkpoint_root = explicit_path(args.checkpoint_root, "checkpoint root")
            if control in json_out.parents or checkpoint_root in json_out.parents:
                raise ArtifactError("declaration may not be written into raw source artifacts")
            payload = predeclare_false_kill(
                control, checkpoint_root, args.target_id
            )
            exit_code = 0
        elif args.command == "prepare-remediation-authorization":
            control = explicit_path(args.control, "control")
            checkpoint_root = explicit_path(args.checkpoint_root, "checkpoint root")
            candidate = explicit_path(args.candidate_run, "candidate run")
            declaration = explicit_file(args.declaration, "false-kill declaration")
            rerun_manifest = explicit_file(
                args.rerun_manifest, "one-task rerun manifest"
            )
            declaration_value = read_json(
                declaration, "false-kill declaration"
            )
            attempt_root = canonical_remediation_attempt_paths(
                checkpoint_root, declaration_value
            )[
                "attempt_root"
            ]
            protected = (control, checkpoint_root, candidate, attempt_root)
            if any(paths_overlap(json_out, path) for path in protected):
                json_out = None
                raise ArtifactError(
                    "--json-out must be outside all sources and the canonical attempt root"
                )
            payload = prepare_remediation_authorization(
                control,
                checkpoint_root,
                candidate,
                declaration,
                rerun_manifest,
                args.rerun_id,
                args.nonce,
            )
            exit_code = 0
        elif args.command == "snapshot-remediation-provenance":
            driver = explicit_file(args.driver, "parallel driver")
            token = explicit_file(
                args.authorization_token, "remediation authorization token"
            )
            raw_provenance = explicit_file(
                args.raw_provenance, "raw N=100 run provenance"
            )
            dataset = explicit_file(args.dataset, "ContextBench dataset")
            manifest = explicit_file(args.manifest, "rerun manifest")
            chdir = explicit_path(args.chdir, "driver working directory")
            rerank_model = (
                explicit_path(args.rerank_model_dir, "rerank model")
                if args.rerank_model_dir
                else None
            )
            if paths_overlap(json_out, token.parent) or paths_overlap(
                json_out, raw_provenance.parent
            ):
                json_out = None
                raise ArtifactError(
                    "prospective provenance output must be outside the canonical "
                    "attempt and raw provenance roots"
                )
            payload = snapshot_authorized_rerun_provenance(
                driver,
                token,
                raw_provenance,
                dataset,
                manifest,
                args.line_budget,
                args.selector_model,
                args.selector_mode,
                rerank_model,
                args.reinclude_tracked_dirs,
                args.query_plans,
                args.timeout,
                chdir,
            )
            exit_code = 0
        elif args.command == "authorize-remediation":
            control = explicit_path(args.control, "control")
            checkpoint_root = explicit_path(args.checkpoint_root, "checkpoint root")
            candidate = explicit_path(args.candidate_run, "candidate run")
            declaration = explicit_file(args.declaration, "false-kill declaration")
            token = explicit_file(
                args.authorization_token, "remediation authorization token"
            )
            prospective = explicit_file(
                args.prospective_provenance, "prospective remediation provenance"
            )
            declaration_value = read_json(
                declaration, "false-kill declaration"
            )
            attempt_root = canonical_remediation_attempt_paths(
                checkpoint_root, declaration_value
            )[
                "attempt_root"
            ]
            if any(
                paths_overlap(json_out, path)
                for path in (control, checkpoint_root, candidate, attempt_root)
            ):
                json_out = None
                raise ArtifactError(
                    "--json-out must be outside all sources and the canonical attempt root"
                )
            payload = authorize_remediation_execution(
                control,
                checkpoint_root,
                candidate,
                declaration,
                token,
                prospective,
            )
            exit_code = 0
        elif args.command == "predeclare-packaging-retry":
            control = explicit_path(args.control, "control")
            checkpoint_root = explicit_path(args.checkpoint_root, "checkpoint root")
            candidate = explicit_path(args.candidate_run, "candidate run")
            declaration = explicit_file(args.declaration, "false-kill declaration")
            authorization = explicit_file(
                args.authorization, "remediation execution authorization"
            )
            rerun = explicit_path(args.rerun, "remediation rerun")
            evaluator = explicit_file(args.evaluator, "evaluator")
            runtime_manifest = explicit_file(
                args.runtime_manifest, "runtime manifest"
            )
            attempt_root = authorization.parent
            if any(
                paths_overlap(json_out, path)
                for path in (control, checkpoint_root, candidate, rerun, attempt_root)
            ):
                json_out = None
                raise ArtifactError(
                    "--json-out must be outside all sources and the canonical attempt root"
                )
            payload = predeclare_packaging_retry(
                control,
                checkpoint_root,
                candidate,
                declaration,
                authorization,
                rerun,
                evaluator,
                runtime_manifest,
                args.failed_packager_sha256,
            )
            exit_code = 0
        elif args.command == "remediate-false-kill":
            control = explicit_path(args.control, "control")
            checkpoint_root = explicit_path(args.checkpoint_root, "checkpoint root")
            candidate = explicit_path(args.candidate_run, "candidate run")
            declaration = explicit_file(args.declaration, "false-kill declaration")
            authorization = explicit_file(
                args.authorization, "remediation execution authorization"
            )
            rerun = explicit_path(args.rerun, "remediation rerun")
            evaluator = explicit_file(args.evaluator, "evaluator")
            runtime_manifest = explicit_file(
                args.runtime_manifest, "runtime manifest"
            )
            cache = explicit_path(
                args.cache_dir, "evaluator cache", must_exist=False
            )
            bundle = explicit_path(
                args.bundle_root, "remediation bundle", must_exist=False
            )
            attempt_root = authorization.parent
            if any(
                paths_overlap(json_out, path)
                for path in (control, checkpoint_root, candidate, rerun, attempt_root)
            ):
                json_out = None
                raise ArtifactError(
                    "--json-out must be outside all sources and the canonical attempt root"
                )
            payload = create_remediation_bundle(
                control,
                checkpoint_root,
                candidate,
                declaration,
                authorization,
                rerun,
                evaluator,
                runtime_manifest,
                cache,
                bundle,
            )
            exit_code = 0 if payload["comparison"]["gate"]["passed"] else 1
        elif args.command == "verify-remediation":
            bundle = explicit_path(args.bundle, "remediation bundle")
            if paths_overlap(json_out, bundle):
                json_out = None
                raise ArtifactError("--json-out may not overlap the remediation bundle")
            payload = verify_remediation_bundle(bundle)
            exit_code = 0
        else:
            raise ArtifactError(f"unhandled command: {args.command}")
        write_output(json_out, payload)
        print(concise_text(payload))
        return exit_code
    except ArtifactError as error:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "mode": getattr(args, "command", "unknown"),
            "valid": False,
            "error": str(error),
        }
        if json_out is not None:
            try:
                write_output(json_out, payload)
            except OSError:
                pass
        print(f"INVALID: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
