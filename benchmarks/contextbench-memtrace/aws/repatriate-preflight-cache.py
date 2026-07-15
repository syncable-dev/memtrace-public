#!/usr/bin/env python3
"""Move sealed repair caches back to their original ContextBench hosts."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd


ADAPTER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ADAPTER_DIR))
import runner as retrieval  # noqa: E402


DEFAULT_CACHE_ROOT = "/srv/contextbench/graph-cache-agent"
DEFAULT_BINARY = "/srv/contextbench/memtrace-bin/memtrace"
PREFLIGHT_REQUIRED_STAGES = (
    "dataset",
    "checkout",
    "index_embeddings",
    "cache_sealed",
    "mcp",
)
SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=30",
)

REMOTE_INSPECTOR = r"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

entry = Path(sys.argv[1])
complete = entry / "complete.json"
if not complete.is_file():
    raise SystemExit(f"missing {complete}")
manifest_bytes = complete.read_bytes()
manifest = json.loads(manifest_bytes)
repo = entry / "repo"
head = subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
status = subprocess.run(
    ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
files = [path for path in entry.rglob("*") if path.is_file()]
print(json.dumps({
    "manifest": manifest,
    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    "checkout_head": head,
    "checkout_status": status,
    "file_count": len(files),
    "file_bytes": sum(path.stat().st_size for path in files),
}))
"""


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def reconcile_destination_evidence(destination_fleet_path: Path) -> dict[str, Any]:
    fleet = load_json(destination_fleet_path)
    manifest = [str(instance_id) for instance_id in fleet.get("source_manifest") or []]
    if not manifest or len(manifest) != len(set(manifest)):
        raise ValueError("destination fleet requires a non-empty unique manifest")
    aggregate = destination_fleet_path.parent / "aggregate-preflight"
    records_dir = aggregate / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    source_ids = set(manifest)
    for proof_path in sorted((aggregate / "repair-proofs").glob("*.json")):
        proof = load_json(proof_path)
        instance_id = str(proof.get("instance_id") or "")
        failed_stages = [
            stage
            for stage in PREFLIGHT_REQUIRED_STAGES
            if (proof.get("stages") or {}).get(stage, {}).get("status") != "PASS"
        ]
        if (
            not instance_id
            or instance_id not in source_ids
            or proof.get("status") != "success"
            or failed_stages
            or not (proof.get("cache") or {}).get("cache_hit")
        ):
            raise ValueError(
                f"invalid destination repair proof: {proof_path} "
                f"instance_id={instance_id!r} status={proof.get('status')} "
                f"failed_stages={failed_stages} "
                f"cache_hit={(proof.get('cache') or {}).get('cache_hit')}"
            )
        atomic_write_json(records_dir / f"{instance_id}.json", proof)

    records: dict[str, dict[str, Any]] = {}
    for record_path in sorted(records_dir.glob("*.json")):
        record = load_json(record_path)
        instance_id = str(record.get("instance_id") or "")
        if not instance_id or instance_id in records:
            raise ValueError(f"invalid or duplicate destination record: {record_path}")
        if instance_id not in source_ids:
            raise ValueError(f"destination record outside source manifest: {instance_id}")
        records[instance_id] = record
    counts = {
        status: sum(record.get("status") == status for record in records.values())
        for status in ("success", "failure", "running")
    }
    summary = {
        "schema_version": 1,
        "total": len(manifest),
        "records": len(records),
        "terminal_records": counts["success"] + counts["failure"],
        "succeeded": counts["success"],
        "failed": counts["failure"],
        "running": counts["running"],
        "captured_at_unix_ns": time.time_ns(),
    }
    atomic_write_json(aggregate / "summary.json", summary)
    return summary


def publish_repair_proof(
    destination_fleet_path: Path, record: dict[str, Any]
) -> dict[str, Any]:
    instance_id = str(record.get("instance_id") or "")
    if not instance_id:
        raise ValueError("destination repair proof has no instance_id")
    proof_path = (
        destination_fleet_path.parent
        / "aggregate-preflight"
        / "repair-proofs"
        / f"{instance_id}.json"
    )
    atomic_write_json(proof_path, record)
    return reconcile_destination_evidence(destination_fleet_path)


def task_assignments(fleet: dict[str, Any]) -> dict[str, dict[str, str]]:
    assignments: dict[str, dict[str, str]] = {}
    for shard_id, shard in fleet.get("shards", {}).items():
        host = str(shard.get("public_ip") or "")
        if not host:
            raise ValueError(f"{shard_id} has no public_ip")
        for instance_id in shard.get("task_ids") or []:
            instance_id = str(instance_id)
            if instance_id in assignments:
                raise ValueError(f"duplicate fleet assignment for {instance_id}")
            assignments[instance_id] = {
                "host": host,
                "preflight_run_id": str(shard.get("preflight_run_id") or ""),
            }
    return assignments


def load_records(records_dir: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(records_dir.glob("*.json")):
        record = load_json(path)
        instance_id = str(record.get("instance_id") or "")
        if not instance_id:
            raise ValueError(f"record has no instance_id: {path}")
        if instance_id in records:
            raise ValueError(f"duplicate record for {instance_id}")
        records[instance_id] = record
    return records


def build_plan(
    dataset: Path,
    repair_fleet: dict[str, Any],
    destination_fleet: dict[str, Any],
    repair_records: dict[str, dict[str, Any]],
    namespace: str,
) -> list[dict[str, str]]:
    frame = pd.read_parquet(
        dataset, columns=["instance_id", "repo_url", "base_commit"]
    )
    rows = {
        str(row.instance_id): row
        for row in frame.itertuples(index=False)
    }
    source_assignments = task_assignments(repair_fleet)
    destination_assignments = task_assignments(destination_fleet)
    manifest = [str(value) for value in repair_fleet.get("source_manifest") or []]
    if not manifest:
        raise ValueError("repair fleet has no source manifest")
    plan: list[dict[str, str]] = []
    for instance_id in manifest:
        if instance_id not in rows:
            raise ValueError(f"repair task is outside dataset: {instance_id}")
        if instance_id not in destination_assignments:
            raise ValueError(f"repair task has no original host: {instance_id}")
        destination = destination_assignments[instance_id]
        if not destination["preflight_run_id"]:
            raise ValueError(
                f"repair task has no original preflight run: {instance_id}"
            )
        record = repair_records.get(instance_id)
        if not record or record.get("status") != "success":
            status = record.get("status") if record else "missing"
            raise ValueError(f"repair task is not successful: {instance_id} ({status})")
        row = rows[instance_id]
        repo_url = str(row.repo_url)
        base_commit = str(row.base_commit)
        plan.append(
            {
                "instance_id": instance_id,
                "repo_url": repo_url,
                "base_commit": base_commit,
                "cache_key": retrieval.graph_cache_key(
                    repo_url, base_commit, namespace
                ),
                "source_host": source_assignments[instance_id]["host"],
                "destination_host": destination["host"],
                "destination_preflight_run_id": destination["preflight_run_id"],
            }
        )
    return plan


def ssh_command(host: str, user: str, command: list[str]) -> list[str]:
    return [
        "ssh",
        *SSH_OPTIONS,
        f"{user}@{host}",
        shlex.join(command),
    ]


def remote_run(
    host: str,
    user: str,
    command: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ssh_command(host, user, command),
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"remote command failed on {host} ({result.returncode}): {result.stderr.strip()}"
        )
    return result


def remote_sha256(host: str, user: str, path: str) -> str:
    output = remote_run(host, user, ["sha256sum", path]).stdout.split()
    if not output:
        raise RuntimeError(f"sha256sum returned no output on {host}: {path}")
    return output[0]


def inspect_cache(host: str, user: str, entry: str) -> dict[str, Any]:
    output = remote_run(
        host, user, ["python3", "-c", REMOTE_INSPECTOR, entry]
    ).stdout
    return json.loads(output)


def validate_cache(
    state: dict[str, Any],
    task: dict[str, str],
    namespace: str,
    binary_sha256: str,
) -> None:
    manifest = state["manifest"]
    expected = {
        "repo_url": task["repo_url"].rstrip("/"),
        "base_commit": task["base_commit"],
        "namespace": namespace,
        "history_days": 0,
        "memtrace_binary_sha256": binary_sha256,
    }
    mismatches = {
        key: (manifest.get(key), value)
        for key, value in expected.items()
        if manifest.get(key) != value
    }
    repository_commit = str((manifest.get("repository") or {}).get("commit_sha") or "")
    if repository_commit != task["base_commit"]:
        mismatches["repository.commit_sha"] = (
            repository_commit,
            task["base_commit"],
        )
    if state.get("checkout_head") != task["base_commit"]:
        mismatches["checkout_head"] = (
            state.get("checkout_head"),
            task["base_commit"],
        )
    if state.get("checkout_status"):
        mismatches["checkout_status"] = (state["checkout_status"], "")
    if int(state.get("file_count", 0) or 0) <= 1:
        mismatches["file_count"] = (state.get("file_count"), ">1")
    if mismatches:
        raise ValueError(
            f"cache identity mismatch for {task['instance_id']}: {mismatches}"
        )


def ensure_fleet_terminal(fleet: dict[str, Any], user: str) -> None:
    for shard_id, shard in fleet.get("shards", {}).items():
        run_id = str(shard.get("preflight_run_id") or "")
        host = str(shard.get("public_ip") or "")
        if not run_id or not host:
            raise ValueError(f"{shard_id} is missing preflight run state")
        exit_code = remote_run(
            host,
            user,
            ["cat", f"/srv/contextbench/preflight/{run_id}/exit_code"],
            check=False,
        )
        if exit_code.returncode != 0 or exit_code.stdout.strip() != "0":
            raise RuntimeError(
                f"{shard_id} preflight is not successfully terminal on {host}"
            )


def stream_copy(
    source_host: str,
    destination_host: str,
    user: str,
    source: str,
    incoming: str,
) -> None:
    remote_run(destination_host, user, ["rm", "-rf", incoming])
    remote_run(destination_host, user, ["mkdir", "-p", incoming])
    source_process = subprocess.Popen(
        ssh_command(source_host, user, ["tar", "-C", source, "-cf", "-", "."]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert source_process.stdout is not None
    destination_process = subprocess.Popen(
        ssh_command(destination_host, user, ["tar", "-C", incoming, "-xf", "-"]),
        stdin=source_process.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    source_process.stdout.close()
    _, destination_stderr = destination_process.communicate()
    source_stderr = source_process.stderr.read() if source_process.stderr else b""
    source_returncode = source_process.wait()
    if source_returncode != 0 or destination_process.returncode != 0:
        remote_run(destination_host, user, ["rm", "-rf", incoming], check=False)
        raise RuntimeError(
            "cache stream failed: "
            f"source rc={source_returncode} {source_stderr.decode(errors='replace').strip()} "
            f"destination rc={destination_process.returncode} "
            f"{destination_stderr.decode(errors='replace').strip()}"
        )


def promote_cache(host: str, user: str, target: str, incoming: str, lock: str) -> None:
    backup = target + ".pre-repair"
    script = r"""
set -euo pipefail
target=$1
incoming=$2
lock=$3
backup=$4
exec 9>"$lock"
flock -w 900 9
rm -rf "$backup"
if [ -d "$target" ]; then mv "$target" "$backup"; fi
mv "$incoming" "$target"
rm -rf "$backup"
"""
    remote_run(
        host,
        user,
        ["bash", "-c", script, "repatriate", target, incoming, lock, backup],
    )


def verify_destination_task(
    task: dict[str, str],
    namespace: str,
    cache_root: str,
    binary: str,
    user: str,
    remote_dataset: str,
) -> dict[str, Any]:
    run_id = task["destination_preflight_run_id"]
    output_dir = f"/srv/contextbench/preflight/{run_id}"
    remote_run(
        task["destination_host"],
        user,
        [
            "/srv/contextbench/venv/bin/python",
            "/home/ubuntu/contextbench-adapter/aws/full_preflight_runner.py",
            "--dataset",
            remote_dataset,
            "--output-dir",
            output_dir,
            "--graph-cache-dir",
            cache_root,
            "--cache-namespace",
            namespace,
            "--memtrace-binary",
            binary,
            "--rerank-model-dir",
            "/srv/contextbench/rerank-model",
            "--instance-id",
            task["instance_id"],
            "--child",
            "--host",
            task["destination_host"],
            "--run-id",
            run_id,
        ],
    )
    record_path = f"{output_dir}/records/{task['instance_id']}.json"
    record = json.loads(
        remote_run(
            task["destination_host"], user, ["cat", record_path]
        ).stdout
    )
    failed_stages = {
        name: stage.get("status")
        for name, stage in (record.get("stages") or {}).items()
        if stage.get("status") != "PASS"
    }
    if record.get("status") != "success" or failed_stages:
        raise RuntimeError(
            f"destination preflight failed for {task['instance_id']}: "
            f"status={record.get('status')} stages={failed_stages}"
        )
    if not (record.get("cache") or {}).get("cache_hit"):
        raise RuntimeError(
            f"destination preflight rebuilt instead of reusing the copied cache: "
            f"{task['instance_id']}"
        )
    return record


def execute_plan(
    plan: list[dict[str, str]],
    destination_fleet_path: Path,
    namespace: str,
    cache_root: str,
    binary: str,
    user: str,
    remote_dataset: str,
) -> None:
    for index, task in enumerate(plan, 1):
        key = task["cache_key"]
        source_entry = f"{cache_root}/{key}"
        target_entry = f"{cache_root}/{key}"
        incoming = f"{cache_root}/.incoming-repair-{key}-{os.getpid()}"
        lock = f"{cache_root}/.locks/{key}.lock"
        source_binary_sha = remote_sha256(task["source_host"], user, binary)
        destination_binary_sha = remote_sha256(
            task["destination_host"], user, binary
        )
        if source_binary_sha != destination_binary_sha:
            raise ValueError(
                f"binary SHA mismatch for {task['instance_id']}: "
                f"{source_binary_sha} != {destination_binary_sha}"
            )
        source_state = inspect_cache(task["source_host"], user, source_entry)
        validate_cache(source_state, task, namespace, source_binary_sha)
        stream_copy(
            task["source_host"],
            task["destination_host"],
            user,
            source_entry,
            incoming,
        )
        incoming_state = inspect_cache(task["destination_host"], user, incoming)
        validate_cache(incoming_state, task, namespace, destination_binary_sha)
        if incoming_state["manifest_sha256"] != source_state["manifest_sha256"]:
            raise ValueError(f"manifest changed in transit for {task['instance_id']}")
        promote_cache(
            task["destination_host"], user, target_entry, incoming, lock
        )
        destination_state = inspect_cache(
            task["destination_host"], user, target_entry
        )
        validate_cache(destination_state, task, namespace, destination_binary_sha)
        if destination_state["manifest_sha256"] != source_state["manifest_sha256"]:
            raise ValueError(f"promoted manifest mismatch for {task['instance_id']}")
        proof = verify_destination_task(
            task,
            namespace,
            cache_root,
            binary,
            user,
            remote_dataset,
        )
        summary = publish_repair_proof(destination_fleet_path, proof)
        print(
            f"[{index}/{len(plan)}] {task['instance_id']} -> "
            f"{task['destination_host']} ({destination_state['file_count']} files, "
            f"cache_hit={proof['cache']['cache_hit']}, mcp=PASS, "
            f"fleet_pass={summary['succeeded']}/{summary['total']})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--repair-fleet", type=Path, required=True)
    parser.add_argument("--destination-fleet", type=Path, required=True)
    parser.add_argument("--repair-records", type=Path, required=True)
    parser.add_argument("--cache-namespace", required=True)
    parser.add_argument("--cache-root", default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--memtrace-binary", default=DEFAULT_BINARY)
    parser.add_argument(
        "--remote-dataset",
        default="/srv/contextbench/contextbench/data/full.parquet",
    )
    parser.add_argument("--ssh-user", default="ubuntu")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    repair_fleet = load_json(args.repair_fleet)
    destination_fleet = load_json(args.destination_fleet)
    plan = build_plan(
        args.dataset,
        repair_fleet,
        destination_fleet,
        load_records(args.repair_records),
        args.cache_namespace,
    )
    print(json.dumps(plan, indent=2))
    if not args.execute:
        print(f"dry run: {len(plan)} sealed caches ready for repatriation")
        return
    ensure_fleet_terminal(repair_fleet, args.ssh_user)
    ensure_fleet_terminal(destination_fleet, args.ssh_user)
    reconcile_destination_evidence(args.destination_fleet)
    execute_plan(
        plan,
        args.destination_fleet,
        args.cache_namespace,
        args.cache_root,
        args.memtrace_binary,
        args.ssh_user,
        args.remote_dataset,
    )


if __name__ == "__main__":
    main()
