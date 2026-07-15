#!/usr/bin/env python3
"""Preflight every ContextBench repository snapshot before the paid Codex run.

Each task gets a separate terminal JSON record. Successful records prove the
exact checkout, embedding-enabled Memtrace index, sealed reusable cache, and a
real task-scoped MCP search. Bulk mode runs isolated single-task child
processes so a timeout can be killed without poisoning the remaining fleet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

import codex_runner


SCHEMA_VERSION = 1
PIN_SHIM_MARKER = "MEMTRACE-TASKSET-SHIM v1"


def utc_unix_ns() -> int:
    return time.time_ns()


def slug(instance_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", instance_id)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def task_query(problem_statement: str) -> str:
    compact = " ".join(str(problem_statement or "").split())
    if not compact:
        return "primary implementation entry point"
    return compact[:600]


def load_row(dataset: Path, instance_id: str) -> dict[str, Any]:
    frame = pd.read_parquet(dataset)
    selected = frame.loc[frame["instance_id"].astype(str) == instance_id]
    if len(selected) != 1:
        raise ValueError(f"expected one row for {instance_id}, found {len(selected)}")
    return selected.iloc[0].to_dict()


def load_manifest(dataset: Path, manifest: Path | None) -> list[str]:
    frame = pd.read_parquet(dataset, columns=["instance_id"])
    known = frame["instance_id"].astype(str).tolist()
    if manifest is None:
        return known
    loaded = json.loads(manifest.read_text())
    task_ids = (
        [str(task["instance_id"]) for task in loaded["tasks"]]
        if isinstance(loaded, dict)
        else [str(instance_id) for instance_id in loaded]
    )
    if not task_ids or len(task_ids) != len(set(task_ids)):
        raise ValueError("manifest must contain unique task ids")
    unknown = sorted(set(task_ids) - set(known))
    if unknown:
        raise ValueError(f"manifest contains tasks absent from dataset: {unknown[:5]}")
    return task_ids


def stage(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}


def complete_manifest_sha(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def install_pin_shim(memtrace_binary: Path) -> Path:
    real_binary = memtrace_binary.with_name("memtrace.real")
    current = memtrace_binary.read_bytes() if memtrace_binary.exists() else b""
    if current.startswith(b"\x7fELF"):
        memtrace_binary.replace(real_binary)
    elif PIN_SHIM_MARKER.encode() in current and real_binary.is_file():
        pass
    else:
        raise RuntimeError(
            f"cannot install pin shim: {memtrace_binary} is neither an ELF binary nor a known shim"
        )
    shim = f'''#!/usr/bin/env bash
# {PIN_SHIM_MARKER} — shared with the ContextBench scored runner.
set -euo pipefail
SELF_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
REAL_BIN="$SELF_DIR/{real_binary.name}"
if [ "${{MEMTRACE_PIN_ENABLE:-0}}" != "1" ] || [ -z "${{MEMTRACE_PIN_SLOTS:-}}" ] || [ -z "${{MEMTRACE_PIN_CORES_PER_SLOT:-}}" ]; then
    exec "$REAL_BIN" "$@"
fi
SLOTS="$MEMTRACE_PIN_SLOTS"
CORES_PER_SLOT="$MEMTRACE_PIN_CORES_PER_SLOT"
LOCKDIR="${{MEMTRACE_PIN_LOCKDIR:-$SELF_DIR/pin-slots}}"
mkdir -p "$LOCKDIR" 2>/dev/null || true
TOTAL_CORES="$(nproc)"
WAIT_SECONDS="${{MEMTRACE_PIN_WAIT_SECONDS:-900}}"
case "$WAIT_SECONDS" in
    ''|*[!0-9]*) echo "memtrace-shim: invalid MEMTRACE_PIN_WAIT_SECONDS=$WAIT_SECONDS" >&2; exit 96 ;;
esac
deadline=$((SECONDS + WAIT_SECONDS))
announced_wait=0
while true; do
    i=0
    while [ "$i" -lt "$SLOTS" ]; do
        lockfile="$LOCKDIR/slot-$i.lock"
        if exec {{LOCKFD}}>"$lockfile" && flock -n "$LOCKFD"; then
            lo=$((i * CORES_PER_SLOT))
            hi=$((lo + CORES_PER_SLOT - 1))
            if [ "$i" -eq $((SLOTS - 1)) ] && [ $((TOTAL_CORES - 1)) -gt "$hi" ]; then
                hi=$((TOTAL_CORES - 1))
            fi
            exec taskset -c "${{lo}}-${{hi}}" "$REAL_BIN" "$@"
        fi
        exec {{LOCKFD}}>&- || true
        i=$((i + 1))
    done
    if [ "$SECONDS" -ge "$deadline" ]; then break; fi
    if [ "$announced_wait" -eq 0 ]; then
        echo "memtrace-shim: all $SLOTS pin slots busy; waiting up to ${{WAIT_SECONDS}}s" >&2
        announced_wait=1
    fi
    sleep 0.25
done
echo "memtrace-shim: FATAL no free pin slot after ${{WAIT_SECONDS}}s — refusing to run unpinned" >&2
exit 97
'''
    temporary = memtrace_binary.with_name(f".{memtrace_binary.name}.new-{os.getpid()}")
    temporary.write_text(shim)
    temporary.chmod(0o755)
    temporary.replace(memtrace_binary)
    real_binary.chmod(0o755)
    return real_binary


def run_one(args: argparse.Namespace) -> int:
    instance_id = args.instance_id
    assert instance_id
    record_path = args.output_dir / "records" / f"{slug(instance_id)}.json"
    started = time.monotonic()
    lock_handle = None
    stages = {
        "dataset": stage("PASS", "required metadata present"),
        "checkout": stage("PENDING", ""),
        "index_embeddings": stage("PENDING", ""),
        "cache_sealed": stage("PENDING", ""),
        "mcp": stage("PENDING", ""),
    }
    try:
        row = load_row(args.dataset, instance_id)
        cache_entry, repo_root, cache_audit, lock_handle = codex_runner.prepare_cache(
            row,
            args.graph_cache_dir,
            args.cache_namespace,
            args.memtrace_binary,
            args.rerank_model_dir,
            0,
        )
        expected_commit = str(row["base_commit"])
        actual_commit = codex_runner.git_output(repo_root, "rev-parse", "HEAD")
        if actual_commit != expected_commit:
            raise RuntimeError(
                f"checkout mismatch: expected {expected_commit}, got {actual_commit}"
            )
        repository = cache_audit["repository"]
        indexed_commit = str(repository.get("commit_sha") or "")
        if indexed_commit != expected_commit:
            raise RuntimeError(
                f"indexed commit mismatch: expected {expected_commit}, got {indexed_commit}"
            )
        stages["checkout"] = stage("PASS", f"exact {expected_commit}; no dirty suffix")

        index = cache_audit.get("index") or {}
        embedding = index.get("embedding") if isinstance(index, dict) else None
        written = int(embedding.get("written", 0) or 0) if isinstance(embedding, dict) else 0
        if not cache_audit.get("cache_hit") and written <= 0:
            raise RuntimeError(f"fresh index wrote no embeddings: {index}")
        index_detail = "compatible embeddings reused" if cache_audit.get("cache_hit") else f"{written} embeddings written"
        stages["index_embeddings"] = stage("PASS", index_detail)

        manifest_path = codex_runner.cache_manifest_path(cache_entry)
        manifest = read_json(manifest_path)
        if (
            manifest.get("repo_url") != str(row["repo_url"]).rstrip("/")
            or manifest.get("base_commit") != expected_commit
            or manifest.get("namespace") != args.cache_namespace
            or manifest.get("history_days") != 0
        ):
            raise RuntimeError(f"cache manifest identity mismatch: {manifest_path}")
        stages["cache_sealed"] = stage(
            "PASS", f"complete.json {complete_manifest_sha(manifest_path)[:16]}"
        )

        env = codex_runner.memtrace_environment(
            repo_root, cache_entry, args.rerank_model_dir, args.memtrace_binary
        )
        client = codex_runner.open_memtrace_client(repo_root, env)
        try:
            result = client.call_tool(
                "find_code",
                {
                    "repo_id": str(repository["repo_id"]),
                    "query": task_query(str(row.get("problem_statement") or "")),
                    "limit": 3,
                    "include_diagnostics": True,
                    "view": "committed",
                    "line_budget": 40,
                },
            )
        finally:
            client.close()
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise RuntimeError(f"task-scoped find_code returned malformed result: {result!r}")
        stages["mcp"] = stage(
            "PASS", f"find_code returned {len(result['results'])} ranked results"
        )
        record = {
            "schema_version": SCHEMA_VERSION,
            "instance_id": instance_id,
            "status": "success",
            "host": args.host,
            "run_id": args.run_id,
            "repo_url": str(row["repo_url"]),
            "base_commit": expected_commit,
            "cache": cache_audit,
            "cache_manifest_sha256": complete_manifest_sha(manifest_path),
            "stages": stages,
            "seconds": round(time.monotonic() - started, 3),
            "completed_at_unix_ns": utc_unix_ns(),
        }
        atomic_write_json(record_path, record)
        return 0
    except Exception as error:
        failing_stage = next(
            (name for name, value in stages.items() if value["status"] == "PENDING"),
            "mcp",
        )
        stages[failing_stage] = stage("FAIL", f"{type(error).__name__}: {error}")
        atomic_write_json(
            record_path,
            {
                "schema_version": SCHEMA_VERSION,
                "instance_id": instance_id,
                "status": "failure",
                "host": args.host,
                "run_id": args.run_id,
                "stages": stages,
                "error": type(error).__name__,
                "detail": str(error),
                "seconds": round(time.monotonic() - started, 3),
                "completed_at_unix_ns": utc_unix_ns(),
            },
        )
        print(f"ERROR {instance_id}: {error}", file=sys.stderr)
        return 1
    finally:
        if lock_handle is not None:
            import fcntl

            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()


def child_command(args: argparse.Namespace, instance_id: str) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--dataset",
        str(args.dataset),
        "--output-dir",
        str(args.output_dir),
        "--graph-cache-dir",
        str(args.graph_cache_dir),
        "--cache-namespace",
        args.cache_namespace,
        "--memtrace-binary",
        str(args.memtrace_binary),
        "--rerank-model-dir",
        str(args.rerank_model_dir),
        "--instance-id",
        instance_id,
        "--child",
    ]
    if args.host:
        command += ["--host", args.host]
    if args.run_id:
        command += ["--run-id", args.run_id]
    return command


def run_child(args: argparse.Namespace, instance_id: str) -> tuple[str, int, str]:
    record_path = args.output_dir / "records" / f"{slug(instance_id)}.json"
    if args.resume and read_json(record_path).get("status") == "success":
        return instance_id, 0, "cached terminal record"
    try:
        completed = subprocess.run(
            child_command(args, instance_id),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=args.task_timeout,
        )
        return instance_id, completed.returncode, completed.stdout[-4000:]
    except subprocess.TimeoutExpired as error:
        atomic_write_json(
            record_path,
            {
                "schema_version": SCHEMA_VERSION,
                "instance_id": instance_id,
                "status": "failure",
                "host": args.host,
                "run_id": args.run_id,
                "stages": {"dataset": stage("PASS", "required metadata present"), "checkout": stage("FAIL", f"preflight timed out after {args.task_timeout}s")},
                "error": "TimeoutExpired",
                "detail": str(error),
                "completed_at_unix_ns": utc_unix_ns(),
            },
        )
        return instance_id, 124, f"timed out after {args.task_timeout}s"


def run_bulk(args: argparse.Namespace) -> int:
    task_ids = load_manifest(args.dataset, args.manifest)
    install_pin_shim(args.memtrace_binary)
    cpu_count = os.cpu_count() or args.workers
    os.environ["MEMTRACE_PIN_ENABLE"] = "1"
    os.environ["MEMTRACE_PIN_SLOTS"] = str(args.workers)
    os.environ["MEMTRACE_PIN_CORES_PER_SLOT"] = str(max(1, cpu_count // args.workers))
    os.environ.setdefault("MEMTRACE_PIN_WAIT_SECONDS", "900")
    os.environ.setdefault("MEMTRACE_MAX_THREADS", str(max(1, cpu_count // args.workers)))
    os.environ.setdefault("MEMTRACE_EMBED_INTRA_OP_THREADS", "4")
    os.environ.setdefault("MEMTRACE_DEV", "1")
    os.environ.setdefault("MEMTRACE_TELEMETRY", "off")
    os.environ.setdefault("MEMTRACE_CORTEX", "off")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        args.output_dir / "manifest.json",
        {"schema_version": 1, "task_ids": task_ids, "dataset": str(args.dataset)},
    )
    failures = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_child, args, instance_id): instance_id for instance_id in task_ids}
        for completed, future in enumerate(as_completed(futures), 1):
            instance_id, returncode, output = future.result()
            if returncode != 0:
                failures += 1
            print(f"[{completed}/{len(task_ids)}] {instance_id} rc={returncode}")
            if returncode != 0 and output:
                print(output, file=sys.stderr)
    records = [read_json(path) for path in sorted((args.output_dir / "records").glob("*.json"))]
    succeeded = sum(record.get("status") == "success" for record in records)
    failed = sum(record.get("status") == "failure" for record in records)
    atomic_write_json(
        args.output_dir / "summary.json",
        {
            "schema_version": 1,
            "total": len(task_ids),
            "terminal_records": len(records),
            "succeeded": succeeded,
            "failed": failed,
            "completed_at_unix_ns": utc_unix_ns(),
        },
    )
    return 1 if failures or failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--graph-cache-dir", type=Path, required=True)
    parser.add_argument("--cache-namespace", required=True)
    parser.add_argument("--memtrace-binary", type=Path, required=True)
    parser.add_argument("--rerank-model-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--task-timeout", type=int, default=7200)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--instance-id")
    parser.add_argument("--host", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.workers < 1 or args.task_timeout < 1:
        raise SystemExit("workers and task timeout must be positive")
    for required in (args.memtrace_binary,):
        if not required.is_file() or not os.access(required, os.X_OK):
            raise SystemExit(f"required executable missing: {required}")
    if not args.rerank_model_dir.is_dir():
        raise SystemExit(f"rerank model directory missing: {args.rerank_model_dir}")
    if args.instance_id:
        return run_one(args)
    return run_bulk(args)


if __name__ == "__main__":
    raise SystemExit(main())
