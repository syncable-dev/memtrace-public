#!/usr/bin/env python3
"""Run runner.py for many instances concurrently, each fully isolated.

Each instance gets its own subprocess, --work-dir, and --output file, so the
per-task repo checkout and embedded MemDB never collide. Afterwards the
per-instance prediction JSONL files are concatenated into one predictions file
and the per-instance audit files are collected into a single audit directory.

Runner subprocesses have isolated process groups for bounded cleanup. Failures
become explicit zero-context records so the merged output retains the manifest
denominator. Resume trusts only terminal, content-hashed artifacts whose
behavior-and-provenance fingerprint still matches; failures are terminal too,
so a resume cannot overwrite a result already eligible for checkpointing.

Runner behavior is forwarded explicitly so disabled policies retain the
legacy command shape while enabled policies are captured in provenance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from post_selector_policy import (
    POST_SELECTOR_POLICY_CHOICES,
    policy_fingerprint,
    policy_manifest,
)

RUNNER = Path(__file__).resolve().parent / "runner.py"
AGENT_RUNNER = Path(__file__).resolve().parent / "agent_runner.py"
CODEX_RUNNER = Path(__file__).resolve().parent / "codex_runner.py"
RUN_RECORD_SCHEMA_VERSION = 1
PROCESS_TERMINATION_GRACE_SECONDS = 5.0
DEFAULT_SELECTOR_POLICY = "replay-v1"
SELECTOR_POLICY_CHOICES = (DEFAULT_SELECTOR_POLICY, "continuation-v2")
SECRET_NAME_PARTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")


def runner_path(args: argparse.Namespace) -> Path:
    lane = getattr(args, "lane", "retrieval")
    if lane == "agent":
        return AGENT_RUNNER
    if lane == "codex":
        return CODEX_RUNNER
    return RUNNER


def slugify(instance_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", instance_id)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_identity(path: Path | None) -> dict[str, Any] | None:
    """Content identity for provenance inputs without embedding their contents."""
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {"path": str(resolved), "exists": False}
    if resolved.is_file():
        return {
            "path": str(resolved),
            "exists": True,
            "kind": "file",
            "bytes": resolved.stat().st_size,
            "sha256": sha256_file(resolved),
        }

    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    for child in sorted(item for item in resolved.rglob("*") if item.is_file()):
        relative = child.relative_to(resolved).as_posix()
        size = child.stat().st_size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256_file(child).encode("ascii"))
        digest.update(b"\0")
        files += 1
        total_bytes += size
    return {
        "path": str(resolved),
        "exists": True,
        "kind": "directory",
        "files": files,
        "bytes": total_bytes,
        "sha256": digest.hexdigest(),
    }


def behavior_environment() -> dict[str, str]:
    """Record behavior-affecting knobs while excluding credentials."""
    prefixes = ("CB_", "MEMTRACE_", "MEMCORTEX_", "RAYON_")
    return {
        name: value
        for name, value in sorted(os.environ.items())
        if name.startswith(prefixes)
        and not any(part in name.upper() for part in SECRET_NAME_PARTS)
    }


def memtrace_provenance(args: argparse.Namespace) -> dict[str, Any]:
    binary = shutil.which("memtrace")
    identity = path_identity(Path(binary)) if binary else None
    adjacent_real = Path(f"{binary}.real") if binary else None
    source_manifest = Path(binary).with_name("source-manifest.json") if binary else None
    return {
        "resolved_binary": binary,
        "binary": identity,
        "adjacent_real_binary": (
            path_identity(adjacent_real)
            if adjacent_real is not None and adjacent_real.is_file()
            else None
        ),
        "source_manifest": (
            path_identity(source_manifest)
            if source_manifest is not None and source_manifest.is_file()
            else None
        ),
        "explicit_provenance": path_identity(args.provenance_file),
    }


def build_run_provenance(args: argparse.Namespace) -> dict[str, Any]:
    model_dir = args.rerank_model_dir
    if model_dir is None:
        model_dir = Path(
            os.environ.get(
                "MEMTRACE_RERANK_MODEL_DIR",
                Path.home() / ".memtrace/rerank-models/ms-marco-MiniLM-L-12-v2",
            )
        )
    post_selector_identity = None
    if args.post_selector_policy != "off":
        post_selector_identity = {
            "fingerprint": policy_fingerprint(line_budget=args.line_budget),
            "manifest": policy_manifest(line_budget=args.line_budget),
        }
    payload = {
        "schema_version": RUN_RECORD_SCHEMA_VERSION,
        "driver": path_identity(Path(__file__)),
        "runner": path_identity(runner_path(args)),
        "python": {
            "executable": str(Path(sys.executable).resolve()),
            "version": list(sys.version_info[:3]),
        },
        "dataset": path_identity(args.dataset),
        "manifest": path_identity(args.manifest),
        "rerank_model": path_identity(model_dir),
        "working_directory": str(args.chdir.expanduser().resolve()),
        "env_file": path_identity(args.chdir / ".env"),
        "memtrace": memtrace_provenance(args),
        "behavior_environment": behavior_environment(),
        "policy": {
            "concurrency": args.concurrency,
            "line_budget": args.line_budget,
            "selector_model": args.selector_model,
            "selector_mode": args.selector_mode,
            "selector_policy": args.selector_policy,
            "post_selector_policy": args.post_selector_policy,
            "post_selector_identity": post_selector_identity,
            "reinclude_tracked_dirs": args.reinclude_tracked_dirs,
            "query_plans": args.query_plans,
            "timeout_seconds": args.timeout,
            "lane": getattr(args, "lane", "retrieval"),
            "agent_model": getattr(args, "agent_model", None),
            "history_days": getattr(args, "history_days", None),
            "agent_localization_policy": {
                "agent": "hierarchy-listwise-v2",
                "codex": "codex-memtrace-skills-v1",
            }.get(getattr(args, "lane", "retrieval")),
        },
    }
    if getattr(args, "lane", "retrieval") == "agent":
        mini_agent_src = (
            args.contextbench_root
            / "agent-frameworks/mini-swe-agent/multi-poly-pro-verified/mini-swe-agent/src"
        )
        payload["agent"] = {
            "contextbench_root": str(args.contextbench_root.resolve()),
            "mini_agent_source": path_identity(mini_agent_src),
            "base_agent_config": path_identity(args.base_agent_config),
            "graph_cache_dir": (
                str(args.graph_cache_dir.resolve()) if args.graph_cache_dir else None
            ),
            "cache_namespace": args.cache_namespace,
        }
    elif getattr(args, "lane", "retrieval") == "codex":
        payload["codex"] = {
            "binary": path_identity(args.codex_binary),
            "skills": path_identity(args.memtrace_skills_dir),
            "memtrace_binary": path_identity(args.memtrace_binary),
            "graph_cache_dir": str(args.graph_cache_dir.resolve()),
            "cache_namespace": args.cache_namespace,
            "final_context_policy": "codex-structured-final-v1",
        }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    with temporary.open("w", encoding="utf-8") as output:
        output.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def atomic_write_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    with temporary.open("w", encoding="utf-8") as output:
        output.write(json.dumps(value, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def valid_prediction(path: Path, instance_id: str) -> tuple[bool, str | None]:
    if not path.is_file() or path.stat().st_size == 0:
        return False, "prediction is missing or empty"
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as error:
        return False, f"prediction is not valid JSONL: {error}"
    if len(rows) != 1 or not isinstance(rows[0], dict):
        return (
            False,
            f"prediction must contain exactly one object; found {len(rows)} rows",
        )
    row = rows[0]
    if str(row.get("instance_id")) != instance_id:
        return False, "prediction instance_id does not match the manifest"
    if not isinstance(row.get("traj_data"), dict):
        return False, "prediction has no traj_data object"
    if row.get("harness_failure"):
        return False, "prediction is a harness failure stub"
    return True, None


def mergeable_prediction(path: Path, instance_id: str) -> tuple[bool, str | None]:
    """Validate either a real prediction or an intentional zero-result stub."""
    if not path.is_file() or path.stat().st_size == 0:
        return False, "prediction record is missing or empty"
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as error:
        return False, f"prediction record is not valid JSONL: {error}"
    if len(rows) != 1 or not isinstance(rows[0], dict):
        return False, f"prediction record must contain one object; found {len(rows)}"
    if str(rows[0].get("instance_id")) != instance_id:
        return False, "prediction record instance_id does not match the manifest"
    if not isinstance(rows[0].get("traj_data"), dict):
        return False, "prediction record has no traj_data object"
    return True, None


def artifact_hash(path: Path) -> str | None:
    return sha256_file(path) if path.is_file() else None


def success_record_matches(
    run_dir: Path,
    output: Path,
    instance_id: str,
    fingerprint: str,
    query_plans: bool,
) -> bool:
    record_path = run_dir / "run_record.json"
    if not record_path.is_file():
        return False
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    valid, _ = valid_prediction(output, instance_id)
    if not valid:
        return False
    audit_path = run_dir / "prediction-audit" / f"{slugify(instance_id)}.json"
    query_plan = run_dir / "query-plan.json"
    audit_sha256 = artifact_hash(audit_path)
    query_plan_sha256 = artifact_hash(query_plan)
    return bool(
        record.get("schema_version") == RUN_RECORD_SCHEMA_VERSION
        and record.get("status") == "success"
        and record.get("instance_id") == instance_id
        and record.get("run_fingerprint") == fingerprint
        and record.get("prediction_sha256") == artifact_hash(output)
        and audit_sha256 is not None
        and record.get("audit_sha256") == audit_sha256
        and (
            not query_plans
            or (
                query_plan_sha256 is not None
                and record.get("query_plan_sha256") == query_plan_sha256
            )
        )
    )


def failure_record_matches(
    run_dir: Path,
    output: Path,
    instance_id: str,
    fingerprint: str,
) -> bool:
    record = prior_run_record(run_dir)
    if record is None:
        return False
    mergeable, _ = mergeable_prediction(output, instance_id)
    if not mergeable:
        return False
    failure_path = run_dir / "failure.json"
    audit_path = run_dir / "prediction-audit" / f"{slugify(instance_id)}.json"
    query_plan_path = run_dir / "query-plan.json"
    try:
        prediction_rows = [
            json.loads(line)
            for line in output.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if (
        len(prediction_rows) != 1
        or not isinstance(failure, dict)
        or not isinstance(audit, dict)
    ):
        return False
    prediction_failure = prediction_rows[0].get("harness_failure")
    audit_failure = audit.get("harness_failure")
    return bool(
        record.get("schema_version") == RUN_RECORD_SCHEMA_VERSION
        and record.get("status") == "failure"
        and record.get("instance_id") == instance_id
        and record.get("run_fingerprint") == fingerprint
        and record.get("failure_kind") == failure.get("kind")
        and record.get("prediction_sha256") == artifact_hash(output)
        and record.get("audit_sha256") == artifact_hash(audit_path)
        and record.get("failure_sha256") == artifact_hash(failure_path)
        and "query_plan_sha256" in record
        and record.get("query_plan_sha256") == artifact_hash(query_plan_path)
        and failure.get("schema_version") == RUN_RECORD_SCHEMA_VERSION
        and failure.get("instance_id") == instance_id
        and failure.get("run_fingerprint") == fingerprint
        and isinstance(prediction_failure, dict)
        and prediction_failure.get("kind") == failure.get("kind")
        and prediction_failure.get("run_fingerprint") == fingerprint
        and audit_failure == failure
    )


def resumable_terminal_record(
    run_dir: Path,
    output: Path,
    instance_id: str,
    fingerprint: str,
    query_plans: bool,
) -> dict[str, Any] | None:
    record = prior_run_record(run_dir)
    if record is None:
        return None
    if record.get("status") == "success" and success_record_matches(
        run_dir,
        output,
        instance_id,
        fingerprint,
        query_plans,
    ):
        return record
    if record.get("status") == "failure" and failure_record_matches(
        run_dir,
        output,
        instance_id,
        fingerprint,
    ):
        return record
    return None


def terminate_process_group(
    proc: subprocess.Popen[Any],
    grace_seconds: float | None = None,
) -> None:
    """Terminate the isolated runner session, including Memtrace descendants."""
    grace = (
        PROCESS_TERMINATION_GRACE_SECONDS if grace_seconds is None else grace_seconds
    )
    if os.name != "posix":
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if proc.poll() is None:
            try:
                proc.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if proc.poll() is None:
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def failure_stub(instance_id: str, failure: dict[str, Any]) -> dict[str, Any]:
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
            "kind": failure["kind"],
            "message": failure["message"],
            "run_fingerprint": failure["run_fingerprint"],
        },
    }


def write_failure(
    run_dir: Path,
    output: Path,
    instance_id: str,
    failure: dict[str, Any],
) -> None:
    if output.is_file() and output.stat().st_size > 0:
        os.replace(output, run_dir / "prediction.partial.jsonl")
    audit_path = run_dir / "prediction-audit" / f"{slugify(instance_id)}.json"
    if audit_path.is_file() and audit_path.stat().st_size > 0:
        os.replace(audit_path, audit_path.with_suffix(".partial.json"))
    atomic_write_jsonl(output, failure_stub(instance_id, failure))
    atomic_write_json(run_dir / "failure.json", failure)
    atomic_write_json(
        audit_path,
        {"harness_failure": failure},
    )
    failure_path = run_dir / "failure.json"
    atomic_write_json(
        run_dir / "run_record.json",
        {
            "schema_version": RUN_RECORD_SCHEMA_VERSION,
            "status": "failure",
            "instance_id": instance_id,
            "run_fingerprint": failure["run_fingerprint"],
            "failure_kind": failure["kind"],
            "prediction_sha256": artifact_hash(output),
            "audit_sha256": artifact_hash(audit_path),
            "query_plan_sha256": artifact_hash(run_dir / "query-plan.json"),
            "failure_sha256": artifact_hash(failure_path),
            "returncode": failure.get("returncode"),
            "seconds": failure.get("seconds"),
            "completed_at_unix_ns": failure.get("completed_at_unix_ns"),
        },
    )


def prior_run_record(run_dir: Path) -> dict[str, Any] | None:
    try:
        record = json.loads((run_dir / "run_record.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def discard_stale_query_plan(run_dir: Path, fingerprint: str, resume: bool) -> None:
    query_plan = run_dir / "query-plan.json"
    if not query_plan.is_file():
        return
    record = prior_run_record(run_dir)
    if (
        resume
        and record is not None
        and record.get("status") == "failure"
        and record.get("run_fingerprint") == fingerprint
        and "query_plan_sha256" in record
        and record.get("query_plan_sha256") == artifact_hash(query_plan)
    ):
        # A same-policy retry may reuse a plan produced before a later-stage
        # timeout. Successful or differently fingerprinted artifacts are
        # archived when they fail resume validation instead of being trusted.
        return
    os.replace(query_plan, run_dir / "query-plan.stale.json")


def run_one(
    instance_id: str,
    args: argparse.Namespace,
    semaphore: threading.Semaphore,
    results: dict[str, dict],
) -> None:
    lane = getattr(args, "lane", "retrieval")
    requires_query_plan = args.query_plans and lane != "codex"
    slug = slugify(instance_id)
    run_dir = args.output_dir / "runs" / slug
    run_dir.mkdir(parents=True, exist_ok=True)
    output = run_dir / "prediction.jsonl"
    log_path = run_dir / "runner.log"
    terminal = (
        resumable_terminal_record(
            run_dir,
            output,
            instance_id,
            args.run_fingerprint,
            requires_query_plan,
        )
        if args.resume
        else None
    )
    if terminal is not None:
        status = terminal["status"]
        results[instance_id] = {
            "status": status,
            "returncode": terminal.get("returncode"),
            "seconds": 0.0,
            "skipped": True,
            "prediction": str(output),
            "log": str(log_path),
        }
        if status == "failure":
            results[instance_id].update(
                {
                    "failure_kind": terminal.get("failure_kind"),
                    "failure": str(run_dir / "failure.json"),
                }
            )
        print(
            f"[skip] {instance_id} ({status} terminal fingerprint matched)",
            flush=True,
        )
        return
    discard_stale_query_plan(run_dir, args.run_fingerprint, args.resume)
    if lane == "agent":
        agent_output = run_dir / "agent-output"
        cmd = [
            sys.executable,
            str(AGENT_RUNNER),
            "--dataset",
            str(args.dataset),
            "--instance-id",
            instance_id,
            "--output-dir",
            str(agent_output),
            "--work-dir",
            str(run_dir / "work"),
            "--contextbench-root",
            str(args.contextbench_root),
            "--agent-python",
            sys.executable,
            "--base-agent-config",
            str(args.base_agent_config),
            "--rerank-model-dir",
            str(args.rerank_model_dir),
            "--query-plan-file",
            str(run_dir / "query-plan.json"),
            "--selector-model",
            args.selector_model or "gpt-5",
            "--agent-model",
            args.agent_model,
            "--line-budget",
            str(args.line_budget),
            "--timeout",
            str(max(1, int(args.timeout) - 120)),
            "--history-days",
            str(args.history_days),
            "--cache-namespace",
            args.cache_namespace,
            "--fail-fast",
        ]
        if args.graph_cache_dir:
            cmd += ["--graph-cache-dir", str(args.graph_cache_dir)]
    elif lane == "codex":
        agent_output = run_dir / "agent-output"
        cmd = [
            sys.executable,
            str(CODEX_RUNNER),
            "--dataset",
            str(args.dataset),
            "--instance-id",
            instance_id,
            "--output-dir",
            str(agent_output),
            "--work-dir",
            str(run_dir / "work"),
            "--rerank-model-dir",
            str(args.rerank_model_dir),
            "--graph-cache-dir",
            str(args.graph_cache_dir),
            "--cache-namespace",
            args.cache_namespace,
            "--memtrace-binary",
            str(args.memtrace_binary),
            "--memtrace-skills-dir",
            str(args.memtrace_skills_dir),
            "--codex-binary",
            str(args.codex_binary),
            "--agent-model",
            args.agent_model,
            "--line-budget",
            str(args.line_budget),
            "--timeout",
            str(max(1, int(args.timeout) - 120)),
            "--history-days",
            str(args.history_days),
            "--fail-fast",
        ]
    else:
        cmd = [
            sys.executable,
            str(RUNNER),
            "--dataset",
            str(args.dataset),
            "--instance-id",
            instance_id,
            "--output",
            str(output),
            "--work-dir",
            str(run_dir / "work"),
            "--line-budget",
            str(args.line_budget),
        ]
        if args.selector_model:
            cmd += ["--selector-model", args.selector_model]
        if args.selector_mode != "default":
            cmd += ["--selector-mode", args.selector_mode]
        if args.selector_policy != DEFAULT_SELECTOR_POLICY:
            cmd += ["--selector-policy", args.selector_policy]
        if args.post_selector_policy != "off":
            cmd += ["--post-selector-policy", args.post_selector_policy]
        if args.rerank_model_dir:
            cmd += ["--rerank-model-dir", str(args.rerank_model_dir)]
        if args.reinclude_tracked_dirs:
            cmd += ["--reinclude-tracked-dirs"]
        if args.query_plans:
            # One plan file per instance run dir: no concurrent writers.
            cmd += ["--query-plan-file", str(run_dir / "query-plan.json")]
        if args.graph_cache_dir:
            cmd += [
                "--graph-cache-dir",
                str(args.graph_cache_dir),
                "--cache-namespace",
                args.cache_namespace,
            ]
    with semaphore:
        started = time.monotonic()
        proc: subprocess.Popen[Any] | None = None
        returncode: int | None = None
        failure_kind: str | None = None
        failure_message: str | None = None
        try:
            with log_path.open("w", encoding="utf-8") as log:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=args.chdir,
                    start_new_session=os.name == "posix",
                )
                try:
                    returncode = proc.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    failure_kind = "timeout"
                    failure_message = f"runner exceeded {args.timeout} seconds"
                    terminate_process_group(proc)
                    returncode = proc.returncode
        except OSError as error:
            failure_kind = "spawn_error"
            failure_message = f"could not start runner: {error}"
        except (
            Exception
        ) as error:  # Keep one broken worker from shrinking the denominator.
            if proc is not None:
                terminate_process_group(proc)
                returncode = proc.returncode
            failure_kind = "driver_error"
            failure_message = f"driver exception: {type(error).__name__}: {error}"

        seconds = round(time.monotonic() - started, 1)
        # Fleet checkpoints merge terminal tasks from independent hosts. Record
        # the wall-clock completion instant inside the terminal artifact so a
        # globally ordered checkpoint does not depend on rsync or poll order.
        completed_at_unix_ns = time.time_ns()
        if lane in ("agent", "codex"):
            source_prediction = run_dir / "agent-output" / "predictions.jsonl"
            source_audit = run_dir / "agent-output" / "audit" / f"{slug}.json"
            audit_path = run_dir / "prediction-audit" / f"{slug}.json"
            if source_prediction.is_file():
                shutil.copy2(source_prediction, output)
            if source_audit.is_file():
                audit_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_audit, audit_path)
        if failure_kind is None and returncode != 0:
            failure_kind = "nonzero_exit"
            failure_message = f"runner exited with return code {returncode}"
        valid, validation_error = valid_prediction(output, instance_id)
        if failure_kind is None and not valid:
            failure_kind = "invalid_prediction"
            failure_message = validation_error or "runner output failed validation"
        audit_path = run_dir / "prediction-audit" / f"{slug}.json"
        query_plan = run_dir / "query-plan.json"
        if failure_kind is None and not audit_path.is_file():
            failure_kind = "invalid_artifacts"
            failure_message = "runner did not write its per-instance audit"
        if (
            failure_kind is None
            and args.query_plans
            and lane != "codex"
            and not query_plan.is_file()
        ):
            failure_kind = "invalid_artifacts"
            failure_message = "runner did not write its requested query plan"

        if failure_kind is not None:
            if proc is not None:
                terminate_process_group(proc)
                returncode = proc.returncode
            failure = {
                "schema_version": RUN_RECORD_SCHEMA_VERSION,
                "instance_id": instance_id,
                "kind": failure_kind,
                "message": failure_message,
                "returncode": returncode,
                "seconds": seconds,
                "completed_at_unix_ns": completed_at_unix_ns,
                "timeout_seconds": args.timeout,
                "run_fingerprint": args.run_fingerprint,
                "command": cmd,
                "log": str(log_path),
            }
            write_failure(run_dir, output, instance_id, failure)
            results[instance_id] = {
                "status": "failure",
                "returncode": returncode,
                "seconds": seconds,
                "failure_kind": failure_kind,
                "failure": str(run_dir / "failure.json"),
                "prediction": str(output),
                "log": str(log_path),
            }
            print(
                f"[FAILED] {instance_id} kind={failure_kind} rc={returncode} {seconds}s",
                flush=True,
            )
            return

        atomic_write_json(
            run_dir / "run_record.json",
            {
                "schema_version": RUN_RECORD_SCHEMA_VERSION,
                "status": "success",
                "instance_id": instance_id,
                "run_fingerprint": args.run_fingerprint,
                "prediction_sha256": artifact_hash(output),
                "audit_sha256": artifact_hash(audit_path),
                "query_plan_sha256": artifact_hash(query_plan),
                "returncode": returncode,
                "seconds": seconds,
                "completed_at_unix_ns": completed_at_unix_ns,
            },
        )
        (run_dir / "failure.json").unlink(missing_ok=True)
        results[instance_id] = {
            "status": "success",
            "returncode": returncode,
            "seconds": seconds,
            "prediction": str(output),
            "log": str(log_path),
        }
        print(f"[ok] {instance_id} rc={returncode} {seconds}s", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="JSON with tasks[].instance_id, or a JSON list of ids",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--lane", choices=("retrieval", "agent", "codex"), default="retrieval"
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--line-budget", type=int, default=80)
    parser.add_argument("--selector-model")
    parser.add_argument(
        "--selector-mode", choices=("default", "guarded"), default="default"
    )
    parser.add_argument(
        "--selector-policy",
        choices=SELECTOR_POLICY_CHOICES,
        default=os.environ.get("CB_SELECTOR_POLICY", DEFAULT_SELECTOR_POLICY),
    )
    parser.add_argument(
        "--post-selector-policy",
        choices=POST_SELECTOR_POLICY_CHOICES,
        default=os.environ.get("CB_POST_SELECTOR_POLICY", "off"),
        help="next-run-only post-selector projection (default off)",
    )
    parser.add_argument("--rerank-model-dir", type=Path)
    parser.add_argument("--contextbench-root", type=Path)
    parser.add_argument("--base-agent-config", type=Path)
    parser.add_argument("--agent-model", default="openai/gpt-5")
    parser.add_argument(
        "--history-days",
        type=int,
        default=30,
        help="optional temporal replay lookback (Codex scored runs pass 0 explicitly)",
    )
    parser.add_argument("--codex-binary", type=Path)
    parser.add_argument("--memtrace-binary", type=Path)
    parser.add_argument("--memtrace-skills-dir", type=Path)
    parser.add_argument("--graph-cache-dir", type=Path)
    parser.add_argument("--cache-namespace", default="contextbench-agent-v1")
    parser.add_argument("--reinclude-tracked-dirs", action="store_true")
    parser.add_argument(
        "--query-plans",
        action="store_true",
        help="give each instance its own --query-plan-file",
    )
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse validated terminal instances whose "
        "recorded provenance fingerprint still matches",
    )
    parser.add_argument(
        "--provenance-file",
        type=Path,
        help="optional source/build manifest included in the resume fingerprint",
    )
    parser.add_argument(
        "--chdir",
        type=Path,
        default=Path.cwd(),
        help="cwd for runner.py (so its .env autoload works)",
    )
    args = parser.parse_args()

    loaded = json.loads(args.manifest.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        instance_ids = [task["instance_id"] for task in loaded["tasks"]]
    else:
        instance_ids = list(loaded)
    instance_ids = [str(instance_id) for instance_id in instance_ids]
    if len(instance_ids) != len(set(instance_ids)):
        raise SystemExit("manifest contains duplicate instance_id values")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be at least 1")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be greater than 0")
    if args.lane == "agent":
        if args.contextbench_root is None or not args.contextbench_root.is_dir():
            raise SystemExit("--contextbench-root is required for the agent lane")
        if args.base_agent_config is None or not args.base_agent_config.is_file():
            raise SystemExit("--base-agent-config is required for the agent lane")
        if args.rerank_model_dir is None:
            raise SystemExit("--rerank-model-dir is required for the agent lane")
        if not args.query_plans:
            raise SystemExit("--query-plans is required for the agent lane")
    elif args.lane == "codex":
        if args.rerank_model_dir is None:
            raise SystemExit("--rerank-model-dir is required for the codex lane")
        if args.graph_cache_dir is None:
            raise SystemExit("--graph-cache-dir is required for the codex lane")
        for name in ("codex_binary", "memtrace_binary"):
            path = getattr(args, name)
            if path is None or not path.is_file():
                raise SystemExit(f"--{name.replace('_', '-')} is required for the codex lane")
        if args.memtrace_skills_dir is None or not args.memtrace_skills_dir.is_dir():
            raise SystemExit("--memtrace-skills-dir is required for the codex lane")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    provenance = build_run_provenance(args)
    args.run_fingerprint = provenance["fingerprint"]
    atomic_write_json(args.output_dir / "run_provenance.json", provenance)

    semaphore = threading.Semaphore(args.concurrency)
    results: dict[str, dict] = {}
    threads = [
        threading.Thread(target=run_one, args=(iid, args, semaphore, results))
        for iid in instance_ids
    ]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = time.monotonic() - started

    predictions_path = args.output_dir / "predictions.jsonl"
    audit_dir = args.output_dir / "predictions-audit"
    audit_dir.mkdir(exist_ok=True)
    recorded = 0
    with predictions_path.open("w", encoding="utf-8") as merged:
        for iid in instance_ids:
            slug = slugify(iid)
            run_dir = args.output_dir / "runs" / slug
            pred = run_dir / "prediction.jsonl"
            mergeable, merge_error = mergeable_prediction(pred, iid)
            result = results.get(iid)
            if (
                result is not None
                and result.get("status") == "success"
                and not success_record_matches(
                    run_dir,
                    pred,
                    iid,
                    args.run_fingerprint,
                    args.query_plans and args.lane != "codex",
                )
            ):
                mergeable = False
                merge_error = "successful worker artifacts failed final hash validation"
            if iid not in results or not mergeable:
                failure = {
                    "schema_version": RUN_RECORD_SCHEMA_VERSION,
                    "instance_id": iid,
                    "kind": "worker_crash"
                    if iid not in results
                    else "invalid_merged_artifact",
                    "message": (
                        "worker exited without recording a result"
                        if iid not in results
                        else merge_error or "prediction could not be merged"
                    ),
                    "returncode": None,
                    "seconds": None,
                    "timeout_seconds": args.timeout,
                    "run_fingerprint": args.run_fingerprint,
                    "command": None,
                    "log": str(run_dir / "runner.log"),
                }
                write_failure(run_dir, pred, iid, failure)
                results[iid] = {
                    "status": "failure",
                    "returncode": None,
                    "seconds": None,
                    "failure_kind": failure["kind"],
                    "failure": str(run_dir / "failure.json"),
                    "prediction": str(pred),
                    "log": str(run_dir / "runner.log"),
                }
                mergeable = True
            if mergeable:
                merged.write(pred.read_text(encoding="utf-8"))
                recorded += 1
            per_audit = pred.parent / "prediction-audit" / f"{slug}.json"
            if per_audit.is_file():
                shutil.copy(per_audit, audit_dir / f"{slug}.json")

    completed = sum(result.get("status") == "success" for result in results.values())
    failures = sum(result.get("status") == "failure" for result in results.values())
    summary = {
        "run_fingerprint": args.run_fingerprint,
        "concurrency": args.concurrency,
        "wall_clock_seconds": round(wall, 1),
        "completed": completed,
        "failed": failures,
        "prediction_records": recorded,
        "total": len(instance_ids),
        "predictions": str(predictions_path),
        "audit_dir": str(audit_dir),
        "per_instance": results,
    }
    (args.output_dir / "driver_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "concurrency",
                    "wall_clock_seconds",
                    "completed",
                    "failed",
                    "prediction_records",
                    "total",
                )
            }
        )
    )
    return 0 if completed == len(instance_ids) and recorded == len(instance_ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
