import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("compare_runs.py")
SPEC = importlib.util.spec_from_file_location("contextbench_compare_runs", MODULE_PATH)
assert SPEC and SPEC.loader
compare_runs = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = compare_runs
SPEC.loader.exec_module(compare_runs)

POLICY_MODULE_PATH = Path(__file__).with_name("post_selector_policy.py")
POLICY_SPEC = importlib.util.spec_from_file_location(
    "contextbench_post_selector_policy", POLICY_MODULE_PATH
)
assert POLICY_SPEC and POLICY_SPEC.loader
post_selector_policy = importlib.util.module_from_spec(POLICY_SPEC)
sys.modules[POLICY_SPEC.name] = post_selector_policy
POLICY_SPEC.loader.exec_module(post_selector_policy)

REAL_N90_PROVENANCE = json.loads(
    r"""{
  "behavior_environment": {
    "CB_PACK_POLICY": "v4",
    "CB_SEARCH_LIMIT": "100",
    "MEMCORTEX_STORE_DIR": "/srv/contextbench/cortex-store",
    "MEMTRACE_CORTEX": "off",
    "MEMTRACE_DEV": "1",
    "MEMTRACE_EMBED_INTRA_OP_THREADS": "4",
    "MEMTRACE_INSTALL_MODE": "source",
    "MEMTRACE_MAX_THREADS": "16",
    "MEMTRACE_PIN_CORES_PER_SLOT": "16",
    "MEMTRACE_PIN_ENABLE": "1",
    "MEMTRACE_PIN_LOCKDIR": "/srv/contextbench/memtrace-bin/pin-slots",
    "MEMTRACE_PIN_SLOTS": "12",
    "MEMTRACE_TELEMETRY": "off"
  },
  "dataset": {
    "bytes": 9558250,
    "exists": true,
    "kind": "file",
    "path": "/srv/contextbench/contextbench/data/contextbench_verified.parquet",
    "sha256": "e9dcfd504cbfb849ac815a79040c793d0d92f94eecc9b5a4ee3e1445a2f8a791"
  },
  "driver": {
    "bytes": 26445,
    "exists": true,
    "kind": "file",
    "path": "/home/ubuntu/contextbench-adapter/parallel_driver.py",
    "sha256": "22f2c88f3f5a58ff3312830e9ec41f43be5780d207c780ece93bb99f0c92a1b3"
  },
  "env_file": {
    "bytes": 241,
    "exists": true,
    "kind": "file",
    "path": "/home/ubuntu/contextbench-adapter/.env",
    "sha256": "a276d12f802938de24b2dda74108c1874e359ea136137fdf7ad7c9c96da3e36a"
  },
  "fingerprint": "4908df181bafe9dcff83a1bccf3b4d2e18fea29087e511195536dc1e3aaec45e",
  "manifest": {
    "bytes": 5844,
    "exists": true,
    "kind": "file",
    "path": "/srv/contextbench/results/run-20260713-3e9a814f-cb100-verified/manifest.json",
    "sha256": "63dfb8a1d996312d06d85b6397b034bbd40b95078cf56c5e9cc79c81fcea1660"
  },
  "memtrace": {
    "adjacent_real_binary": {
      "bytes": 103598040,
      "exists": true,
      "kind": "file",
      "path": "/srv/contextbench/memtrace-bin/memtrace.real",
      "sha256": "a76ebc2c3119d546e218a93b841b62a337a0e2e592b2d80a556e7285f873f881"
    },
    "binary": {
      "bytes": 1783,
      "exists": true,
      "kind": "file",
      "path": "/srv/contextbench/memtrace-bin/memtrace",
      "sha256": "b22e9251d8df090a091e6375e4c2248ca5649d9f653c69248baaeef91f300df9"
    },
    "explicit_provenance": null,
    "resolved_binary": "/srv/contextbench/memtrace-bin/memtrace",
    "source_manifest": {
      "bytes": 1164,
      "exists": true,
      "kind": "file",
      "path": "/srv/contextbench/memtrace-bin/source-manifest.json",
      "sha256": "7a581402c2f67b874f07d21bb0c6db6341ea0cbc2be3d5592bc69f1d7919dc7d"
    }
  },
  "policy": {
    "concurrency": 12,
    "line_budget": 200,
    "query_plans": true,
    "reinclude_tracked_dirs": true,
    "selector_mode": "guarded",
    "selector_model": "gpt-5",
    "timeout_seconds": 7200
  },
  "python": {
    "executable": "/usr/bin/python3.11",
    "version": [3, 11, 15]
  },
  "rerank_model": {
    "bytes": 35023294,
    "exists": true,
    "files": 2,
    "kind": "directory",
    "path": "/srv/contextbench/rerank-model",
    "sha256": "434d419188f11d205a42a62125c7b61567800c425fe3604fd507ca3cc47d8614"
  },
  "runner": {
    "bytes": 101926,
    "exists": true,
    "kind": "file",
    "path": "/home/ubuntu/contextbench-adapter/runner.py",
    "sha256": "209c88e2517b250b011abfb7c04dba3224e936e6708d1d988ed9ebe358942658"
  },
  "schema_version": 1,
  "working_directory": "/home/ubuntu/contextbench-adapter"
}"""
)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def prediction(instance_id, failure=False):
    row = {"instance_id": instance_id, "traj_data": {"pred_steps": []}}
    if failure:
        row["traj_data"] = {
            "pred_steps": [],
            "pred_files": [],
            "pred_spans": {},
            "pred_symbols": {},
        }
        row["model_patch"] = ""
        row["harness_failure"] = {
            "kind": "timeout",
            "message": "timeout",
            "run_fingerprint": "fp-candidate",
        }
    return row


def result(instance_id, value):
    metric = {"coverage": value, "precision": value}
    return {
        "instance_id": instance_id,
        "final": {"file": metric, "symbol": metric, "line": metric},
        "trajectory": {"auc_coverage": {"line": value}},
    }


def provenance(root, fingerprint, manifest_bytes, source):
    return {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "manifest": {
            "path": str(root / "manifest.json"),
            "bytes": len(manifest_bytes),
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
        "memtrace": {"source": source},
        "policy": {"line_budget": 200, "selector_model": "gpt-5"},
    }


def write_metadata(root, manifest_bytes, source):
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_bytes(manifest_bytes)
    write_json(
        root / "run_meta.json",
        {
            "run_id": f"run-{source}",
            "cache_namespace": f"cache-{source}",
            "memtrace_source": {"sha": source},
            "line_budget": 200,
            "selector_model": "gpt-5",
        },
    )
    write_json(
        root / "run_provenance.json",
        provenance(root, f"fp-{source}", manifest_bytes, source),
    )


def write_full_run(root, ids, value=0.2, source="control"):
    manifest_bytes = (json.dumps(ids, separators=(",", ":")) + "\n").encode()
    write_metadata(root, manifest_bytes, source)
    predictions = [prediction(instance_id) for instance_id in ids]
    results = [result(instance_id, value) for instance_id in ids]
    write_jsonl(root / "predictions.jsonl", predictions)
    write_jsonl(root / "results.jsonl", results)
    (root / "evaluation-failures.jsonl").write_text("", encoding="utf-8")
    report = {
        "leaderboard": {
            "file": {"recall": value, "precision": value, "f1": value},
            "block": {"recall": value, "precision": value, "f1": value},
            "line": {"recall": value, "precision": value, "f1": value},
            "pass_at_1": None,
        },
        "completeness": {
            "manifest_instances": len(ids),
            "predictions": len(ids),
            "evaluation_results": len(ids),
            "harness_failure_predictions": 0,
            "evaluator_error_results": 0,
            "zero_accounted_instances": 0,
            "zero_accounted_reasons": {},
            "pass_results": 0,
            "allow_partial": False,
            "artifact_complete": True,
            "source_artifact_complete": True,
            "score_publishable": True,
            "missing_predictions": [],
            "invalid_predictions": [],
            "missing_evaluation_results": [],
            "invalid_evaluation_results": [],
            "evaluation_reconciled_failure_predictions": [],
            "missing_pass_results": [],
        },
        "metric_contract": dict(compare_runs.EXPECTED_METRIC_CONTRACT),
    }
    write_json(root / "leaderboard-report.json", report)
    records = {
        instance_id: {"status": "success", "seconds": index + 1}
        for index, instance_id in enumerate(ids)
    }
    write_json(
        root / "driver_summary.json",
        {
            "completed": len(ids),
            "failed": 0,
            "prediction_records": len(ids),
            "total": len(ids),
            "wall_clock_seconds": 1000,
            "per_instance": records,
        },
    )
    (root / "watchdog.log").write_text("", encoding="utf-8")
    triage_rows = [
        {
            "instance_id": instance_id,
            "language": "python",
            "primary": "SPAN",
            "metrics": {
                granularity: {"recall": value, "precision": value, "f1": value}
                for granularity in compare_runs.GRANULARITIES
            },
        }
        for instance_id in ids
    ]
    write_jsonl(root / "triage/triage.jsonl", triage_rows)
    write_json(
        root / "triage/triage_summary.json",
        {
            "num_tasks": len(ids),
            "macro_line_f1": round(value, 4),
            "layer_counts": {"SPAN": len(ids)},
            "by_language": {"python": {"SPAN": len(ids)}},
            "untriaged_instances": [],
            "budget_check": {"ok": True},
        },
    )
    return manifest_bytes


def write_candidate_task(root, instance_id, index, *, failed=False, mtime_second=None):
    run_dir = root / "runs" / instance_id
    run_dir.mkdir(parents=True, exist_ok=True)
    row = prediction(instance_id, failure=failed)
    write_jsonl(run_dir / "prediction.jsonl", [row])
    prediction_hash = hashlib.sha256(
        (run_dir / "prediction.jsonl").read_bytes()
    ).hexdigest()
    audit_path = run_dir / "prediction-audit" / f"{instance_id}.json"
    query_plan = run_dir / "query-plan.json"
    failure_path = run_dir / "failure.json"
    if failed:
        failure = {
            "schema_version": 1,
            "instance_id": instance_id,
            "run_fingerprint": "fp-candidate",
            "kind": "timeout",
            "message": "timeout",
            "returncode": -15,
            "seconds": index + 0.5,
        }
        write_json(audit_path, {"harness_failure": failure})
        query_plan.unlink(missing_ok=True)
    else:
        write_json(audit_path, {"searches": []})
        write_json(query_plan, {instance_id: ["query"]})
        failure_path.unlink(missing_ok=True)
    record = {
        "schema_version": 1,
        "status": "failure" if failed else "success",
        "instance_id": instance_id,
        "run_fingerprint": "fp-candidate",
        "prediction_sha256": prediction_hash,
        "seconds": index + 0.5,
    }
    if failed:
        record["failure_kind"] = "timeout"
        write_json(failure_path, failure)
        record["audit_sha256"] = hashlib.sha256(audit_path.read_bytes()).hexdigest()
        record["query_plan_sha256"] = None
        record["failure_sha256"] = hashlib.sha256(failure_path.read_bytes()).hexdigest()
    else:
        record["audit_sha256"] = hashlib.sha256(audit_path.read_bytes()).hexdigest()
        record["query_plan_sha256"] = hashlib.sha256(
            query_plan.read_bytes()
        ).hexdigest()
    write_json(run_dir / "run_record.json", record)
    mtime = 1_000_000_000 * (mtime_second if mtime_second is not None else index + 1)
    os.utime(run_dir / "run_record.json", ns=(mtime, mtime))


def make_legacy_nonzero_failure(root, instance_id):
    run_dir = root / "runs" / instance_id
    record_path = run_dir / "run_record.json"
    mtime = record_path.stat().st_mtime_ns
    fingerprint = "fp-candidate"
    failure = {
        "schema_version": 1,
        "instance_id": instance_id,
        "kind": "nonzero_exit",
        "message": "runner exited with return code -9",
        "returncode": -9,
        "seconds": 5406.4,
        "timeout_seconds": 7200,
        "run_fingerprint": fingerprint,
        "command": ["python", "runner.py", "--instance-id", instance_id],
        "log": f"/results/{instance_id}/runner.log",
    }
    write_json(run_dir / "failure.json", failure)
    write_json(
        run_dir / "prediction-audit" / f"{instance_id}.json",
        {"harness_failure": failure},
    )
    row = prediction(instance_id, failure=True)
    row["harness_failure"] = {
        "kind": failure["kind"],
        "message": failure["message"],
        "run_fingerprint": fingerprint,
    }
    prediction_path = run_dir / "prediction.jsonl"
    write_jsonl(prediction_path, [row])
    write_json(
        record_path,
        {
            "schema_version": 1,
            "status": "failure",
            "instance_id": instance_id,
            "run_fingerprint": fingerprint,
            "failure_kind": failure["kind"],
            "prediction_sha256": hashlib.sha256(
                prediction_path.read_bytes()
            ).hexdigest(),
        },
    )
    (run_dir / "query-plan.json").unlink(missing_ok=True)
    os.utime(record_path, ns=(mtime, mtime))


def write_partial_candidate(root, manifest_bytes, ids, count=25):
    write_metadata(root, manifest_bytes, "candidate")
    (root / "watchdog.log").write_text("", encoding="utf-8")
    for index, instance_id in enumerate(ids[:count]):
        write_candidate_task(root, instance_id, index, failed=index == 4)


def apply_offline_packing_v2_treatment(control, candidate):
    control_meta = json.loads((control / "run_meta.json").read_text())
    candidate_meta = json.loads((candidate / "run_meta.json").read_text())
    control_meta["manifest_source_run_id"] = "run-control-manifest-source"
    candidate_meta["manifest_source_run_id"] = "run-candidate-manifest-source"
    candidate_meta["post_selector_policy"] = (
        compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2
    )
    write_json(control / "run_meta.json", control_meta)
    write_json(candidate / "run_meta.json", candidate_meta)

    control_provenance = json.loads(json.dumps(REAL_N90_PROVENANCE))
    candidate_provenance = json.loads(json.dumps(control_provenance))
    for root, provenance, fingerprint in (
        (control, control_provenance, "fp-control"),
        (candidate, candidate_provenance, "fp-candidate"),
    ):
        manifest_bytes = (root / "manifest.json").read_bytes()
        provenance["fingerprint"] = fingerprint
        provenance["manifest"] = {
            "path": str(root / "manifest.json"),
            "bytes": len(manifest_bytes),
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
        write_json(root / "run_provenance.json", provenance)

    contract = compare_runs.OFFLINE_PACKING_V2_TREATMENT_CONTRACT
    candidate_provenance["behavior_environment"]["CB_POST_SELECTOR_POLICY"] = (
        compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2
    )
    candidate_provenance["driver"]["bytes"] = contract["candidate_adapter"][
        "driver_bytes"
    ]
    candidate_provenance["driver"]["sha256"] = contract["candidate_adapter"][
        "driver_sha256"
    ]
    candidate_provenance["runner"]["bytes"] = contract["candidate_adapter"][
        "runner_bytes"
    ]
    candidate_provenance["runner"]["sha256"] = contract["candidate_adapter"][
        "runner_sha256"
    ]
    manifest = post_selector_policy.policy_manifest(line_budget=200)
    # The comparator validates the immutable v2 treatment that produced the
    # sealed run. Later v3/v4 additions changed this module's file digest but
    # not v2 behavior, so the fixture must bind the historical implementation
    # identity instead of whatever bytes happen to be in today's module.
    manifest["implementation"]["sha256"] = contract[
        "post_selector_implementation_sha256"
    ]
    candidate_provenance["policy"].update(
        {
            "selector_policy": contract["selector_policy"],
            "post_selector_policy": (
                compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2
            ),
            "post_selector_identity": {
                "fingerprint": contract["post_selector_fingerprint"],
                "manifest": manifest,
            },
        }
    )
    write_json(candidate / "run_provenance.json", candidate_provenance)


def write_runtime_manifest(path, evaluator, checkout=Path("/private/tmp/contextbench")):
    write_json(
        path,
        {
            "schema_version": 1,
            "gold_parquet": {
                "path": "/private/tmp/contextbench/data/contextbench_verified.parquet",
                "sha256": compare_runs.PINNED_GOLD_SHA256,
            },
            "contextbench_checkout": {
                "path": str(checkout),
                "commit": compare_runs.PINNED_CONTEXTBENCH_COMMIT,
                "clean": True,
            },
            "evaluator": {
                "path": str(evaluator),
                "sha256": hashlib.sha256(evaluator.read_bytes()).hexdigest(),
            },
            "python": {
                "executable": "/private/tmp/contextbench-checkpoint-venv311-20260714/bin/python",
                "version": compare_runs.PINNED_PYTHON_VERSION,
            },
            "dependencies": dict(compare_runs.PINNED_DEPENDENCIES),
            "environment": {
                "SYMBOL_DETAIL_MAX": "0",
                "PYTHONPATH": str(checkout),
            },
            "invocation_template": [
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
            ],
        },
    )


def write_remediation_rerun(raw_checkpoint, rerun, target_id):
    manifest_bytes = (json.dumps([target_id], separators=(",", ":")) + "\n").encode()
    rerun.mkdir(parents=True, exist_ok=True)
    (rerun / "manifest.json").write_bytes(manifest_bytes)
    product = {
        "head_sha": "product-sha",
        "binary_sha256": "a" * 64,
    }
    raw_meta = {
        "run_id": "raw-run",
        "concurrency": 12,
        "manifest_instances": 100,
        "selector_model": "gpt-5",
        "selector_mode": "guarded",
        "line_budget": 200,
        "timeout": 7200,
        "cache_namespace": "same-cache",
        "memtrace_source": product,
        "core_pinning": {
            "enabled": True,
            "slots": 12,
            "cores_per_slot": 16,
            "memtrace_max_threads": 16,
        },
    }
    raw_provenance = {
        "schema_version": 1,
        "fingerprint": "fp-raw",
        "manifest": {"path": "/raw/manifest.json", "bytes": 1, "sha256": "raw"},
        "policy": {
            "concurrency": 12,
            "line_budget": 200,
            "selector_model": "gpt-5",
            "selector_mode": "guarded",
            "selector_policy": "replay-v1",
            "post_selector_policy": "off",
            "post_selector_identity": None,
        },
        "behavior_environment": {"MEMTRACE_PIN_SLOTS": "12"},
        "memtrace": {"binary": {"sha256": "b" * 64}},
        "runner": {"sha256": "c" * 64},
        "driver": {"sha256": "d" * 64},
    }
    raw_provenance["fingerprint"] = compare_runs.canonical_sha256(
        compare_runs._drop_keys(raw_provenance, {"fingerprint"})
    )
    write_json(raw_checkpoint / "run_meta.json", raw_meta)
    write_json(raw_checkpoint / "run_provenance.json", raw_provenance)
    write_json(raw_checkpoint / "checkpoint.json", {"schema_version": 1})
    rerun_meta = json.loads(json.dumps(raw_meta))
    rerun_meta.update({"run_id": "rerun-1", "concurrency": 1, "manifest_instances": 1})
    rerun_meta["core_pinning"]["slots"] = 1
    write_json(rerun / "run_meta.json", rerun_meta)
    rerun_provenance = json.loads(json.dumps(raw_provenance))
    rerun_provenance["manifest"] = {
        "path": "/rerun/manifest.json",
        "bytes": len(manifest_bytes),
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    rerun_provenance["policy"]["concurrency"] = 1
    rerun_provenance["behavior_environment"]["MEMTRACE_PIN_SLOTS"] = "1"
    rerun_provenance["fingerprint"] = compare_runs.canonical_sha256(
        compare_runs._drop_keys(rerun_provenance, {"fingerprint"})
    )
    rerun_fingerprint = rerun_provenance["fingerprint"]
    write_json(rerun / "run_provenance.json", rerun_provenance)
    write_candidate_task(rerun, target_id, 0)
    record_path = rerun / "runs" / target_id / "run_record.json"
    record = json.loads(record_path.read_text())
    record["run_fingerprint"] = rerun_fingerprint
    record["returncode"] = 0
    write_json(record_path, record)
    write_jsonl(rerun / "predictions.jsonl", [prediction(target_id)])
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", target_id)
    task_audit = rerun / "runs" / slug / "prediction-audit" / f"{slug}.json"
    (rerun / "predictions-audit").mkdir()
    shutil.copy(task_audit, rerun / "predictions-audit" / f"{slug}.json")
    (rerun / "runs" / slug / "runner.log").write_text("runner\n", encoding="utf-8")
    write_json(
        rerun / "driver_summary.json",
        {
            "run_fingerprint": rerun_fingerprint,
            "concurrency": 1,
            "wall_clock_seconds": 0.5,
            "completed": 1,
            "failed": 0,
            "prediction_records": 1,
            "total": 1,
            "per_instance": {
                target_id: {
                    "status": "success",
                    "returncode": 0,
                    "seconds": 0.5,
                }
            },
        },
    )
    (rerun / "watchdog.log").write_text("", encoding="utf-8")


def write_remediation_authorization(
    raw_checkpoint,
    attempt_root,
    target_id,
    *,
    declaration_path=None,
    origin_checkpoint=None,
):
    rerun = attempt_root / "rerun"
    paths = {
        "attempt_root": attempt_root,
        "token": attempt_root / "authorization-token.json",
        "manifest": attempt_root / "rerun-manifest.json",
        "authorized_provenance": attempt_root / "authorized-run-provenance.json",
        "authorization": attempt_root / "authorization.json",
        "ledger": attempt_root / "attempt-ledger.jsonl",
        "rerun": rerun,
        "bundle": attempt_root / "bundle",
    }
    manifest_bytes = (rerun / "manifest.json").read_bytes()
    shutil.copy(rerun / "manifest.json", paths["manifest"])
    declaration = json.loads(declaration_path.read_text()) if declaration_path else None
    origin = origin_checkpoint or raw_checkpoint
    origin_manifest = origin / "batch-manifest.json"
    raw_manifest = raw_checkpoint / "manifest.json"
    raw_provenance, raw_provenance_binding = (
        compare_runs.remediation_raw_provenance_binding(
            raw_checkpoint / "run_provenance.json"
        )
    )
    token_core = {
        "schema_version": 1,
        "mode": compare_runs.REMEDIATION_TOKEN_MODE,
        "nonce": "n" * 32,
        "target_id": target_id,
        "declaration": {
            "declaration_sha256": (
                declaration["declaration_sha256"] if declaration else "d" * 64
            ),
            "file_sha256": (
                hashlib.sha256(declaration_path.read_bytes()).hexdigest()
                if declaration_path
                else "e" * 64
            ),
        },
        "raw_n_100": {
            "checkpoint_sha256": hashlib.sha256(
                (raw_checkpoint / "checkpoint.json").read_bytes()
            ).hexdigest(),
            "candidate_run_id": "raw-run",
            "candidate_fingerprint": raw_provenance["fingerprint"],
            "manifest_sha256": (
                hashlib.sha256(raw_manifest.read_bytes()).hexdigest()
                if raw_manifest.is_file()
                else "m" * 64
            ),
            "run_provenance_sha256": raw_provenance_binding["file_sha256"],
            "run_provenance_bytes": raw_provenance_binding["bytes"],
            "policy_semantics": raw_provenance_binding["policy_semantics"],
        },
        "originating_batch": {
            "count": (
                len(json.loads((origin / "manifest.json").read_text()))
                if (origin / "manifest.json").is_file()
                else 50
            ),
            "checkpoint_sha256": hashlib.sha256(
                (origin / "checkpoint.json").read_bytes()
            ).hexdigest(),
            "batch_manifest_sha256": (
                hashlib.sha256(origin_manifest.read_bytes()).hexdigest()
                if origin_manifest.is_file()
                else "b" * 64
            ),
        },
        "rerun": {
            "run_id": "rerun-1",
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
    token = {
        **token_core,
        "token_sha256": compare_runs.canonical_sha256(token_core),
    }
    write_json(paths["token"], token)
    provenance = json.loads((rerun / "run_provenance.json").read_text())
    provenance["memtrace"]["explicit_provenance"] = {
        "path": "/remote/authorization-token.json",
        "exists": True,
        "kind": "file",
        "bytes": paths["token"].stat().st_size,
        "sha256": hashlib.sha256(paths["token"].read_bytes()).hexdigest(),
    }
    provenance["fingerprint"] = compare_runs.canonical_sha256(
        compare_runs._drop_keys(provenance, {"fingerprint"})
    )
    rerun_fingerprint = provenance["fingerprint"]
    write_json(rerun / "run_provenance.json", provenance)
    write_json(paths["authorized_provenance"], provenance)
    rerun_record_path = rerun / "runs" / target_id / "run_record.json"
    rerun_record = json.loads(rerun_record_path.read_text())
    rerun_record["run_fingerprint"] = rerun_fingerprint
    write_json(rerun_record_path, rerun_record)
    driver_summary_path = rerun / "driver_summary.json"
    driver_summary = json.loads(driver_summary_path.read_text())
    driver_summary["run_fingerprint"] = rerun_fingerprint
    write_json(driver_summary_path, driver_summary)
    authorization_core = {
        "schema_version": 1,
        "mode": compare_runs.REMEDIATION_AUTHORIZATION_MODE,
        "binding": compare_runs._authorization_binding_from_token(token),
        "token_sha256": token["token_sha256"],
        "token_file_sha256": hashlib.sha256(paths["token"].read_bytes()).hexdigest(),
        "authorized_provenance_sha256": hashlib.sha256(
            paths["authorized_provenance"].read_bytes()
        ).hexdigest(),
        "rerun_fingerprint": rerun_fingerprint,
        "canonical_paths": token["canonical_paths"],
        "protocol": {
            "execution_may_begin_after_this_authorization": True,
            "resume_allowed": False,
            "attempts_allowed": 1,
            "append_only_ledger_required": True,
        },
    }
    authorization = {
        **authorization_core,
        "authorization_sha256": compare_runs.canonical_sha256(authorization_core),
    }
    write_json(paths["authorization"], authorization)
    ledger_core = {
        "schema_version": 1,
        "mode": compare_runs.REMEDIATION_LEDGER_MODE,
        "sequence": 1,
        "event": "execution_authorized",
        "authorization_sha256": authorization["authorization_sha256"],
        "authorization_file_sha256": hashlib.sha256(
            paths["authorization"].read_bytes()
        ).hexdigest(),
        "binding": authorization["binding"],
        "rerun_fingerprint": rerun_fingerprint,
    }
    write_jsonl(
        paths["ledger"],
        [
            {
                **ledger_core,
                "entry_sha256": compare_runs.canonical_sha256(ledger_core),
            }
        ],
    )
    return paths["authorization"]


def reseal_remediation_bundle(bundle, changed_relative):
    seal_path = bundle / "remediation.json"
    seal = json.loads(seal_path.read_text())
    digest = hashlib.sha256((bundle / changed_relative).read_bytes()).hexdigest()
    seal["sealed_files"][changed_relative] = digest
    seal["bundle_binding"]["sealed_files"] = seal["sealed_files"]
    if changed_relative == "comparison.json":
        seal["bundle_binding"]["comparison_sha256"] = digest
    seal["bundle_binding_sha256"] = compare_runs.canonical_sha256(
        seal["bundle_binding"]
    )
    write_json(seal_path, seal)


class CompareRunsTests(unittest.TestCase):
    def test_offline_packing_treatment_is_explicit_and_records_exact_differences(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids, count=10)
            apply_offline_packing_v2_treatment(control, candidate)

            with self.assertRaisesRegex(compare_runs.ArtifactError, "parity failed"):
                compare_runs.validate_parity(control, candidate)
            parity = compare_runs.validate_parity(
                control,
                candidate,
                compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2,
            )

            treatment = parity["candidate_treatment"]
            self.assertEqual(
                treatment["name"],
                compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2,
            )
            self.assertEqual(
                set(treatment["exact_differences"]["run_meta"]),
                compare_runs.OFFLINE_PACKING_V2_RUN_META_DIFFERENCE_PATHS,
            )
            self.assertEqual(
                set(treatment["exact_differences"]["run_provenance"]),
                compare_runs.OFFLINE_PACKING_V2_PROVENANCE_DIFFERENCE_PATHS,
            )
            self.assertEqual(
                treatment["contract"]["post_selector_implementation_sha256"],
                compare_runs.OFFLINE_PACKING_V2_TREATMENT_CONTRACT[
                    "post_selector_implementation_sha256"
                ],
            )

    def test_offline_packing_treatment_rejects_unrelated_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids, count=10)
            apply_offline_packing_v2_treatment(control, candidate)

            candidate_meta = json.loads((candidate / "run_meta.json").read_text())
            candidate_meta["timeout_seconds"] = 1
            write_json(candidate / "run_meta.json", candidate_meta)
            with self.assertRaisesRegex(
                compare_runs.ArtifactError,
                "unexpected run_meta differences",
            ):
                compare_runs.validate_parity(
                    control,
                    candidate,
                    compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2,
                )

            candidate_meta.pop("timeout_seconds")
            candidate_meta["manifest_source_run_id"] = 123
            write_json(candidate / "run_meta.json", candidate_meta)
            with self.assertRaisesRegex(
                compare_runs.ArtifactError,
                "run_meta treatment contract failed",
            ):
                compare_runs.validate_parity(
                    control,
                    candidate,
                    compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2,
                )

            apply_offline_packing_v2_treatment(control, candidate)
            candidate_provenance = json.loads(
                (candidate / "run_provenance.json").read_text()
            )
            candidate_provenance["behavior_environment"]["MEMTRACE_MAX_THREADS"] = "99"
            write_json(candidate / "run_provenance.json", candidate_provenance)
            with self.assertRaisesRegex(
                compare_runs.ArtifactError,
                "unexpected run provenance differences",
            ):
                compare_runs.validate_parity(
                    control,
                    candidate,
                    compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2,
                )

    def test_offline_packing_treatment_rejects_wrong_bound_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids, count=10)
            apply_offline_packing_v2_treatment(control, candidate)

            provenance = json.loads((candidate / "run_provenance.json").read_text())
            provenance["runner"]["sha256"] = "0" * 64
            write_json(candidate / "run_provenance.json", provenance)
            with self.assertRaisesRegex(
                compare_runs.ArtifactError,
                "not the pinned treatment adapter",
            ):
                compare_runs.validate_parity(
                    control,
                    candidate,
                    compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2,
                )

            apply_offline_packing_v2_treatment(control, candidate)
            provenance = json.loads((candidate / "run_provenance.json").read_text())
            identity = provenance["policy"]["post_selector_identity"]
            identity["manifest"]["runtime_parameters"]["line_budget"] = 199
            write_json(candidate / "run_provenance.json", provenance)
            with self.assertRaisesRegex(
                compare_runs.ArtifactError,
                "policy identity binding failed",
            ):
                compare_runs.validate_parity(
                    control,
                    candidate,
                    compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2,
                )

    def test_treatment_checkpoint_requires_explicit_freeze_and_seal_acknowledgement(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            checkpoints = base / "checkpoints"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids, count=10)
            apply_offline_packing_v2_treatment(control, candidate)

            with self.assertRaisesRegex(compare_runs.ArtifactError, "parity failed"):
                compare_runs.freeze_checkpoints(control, candidate, checkpoints)
            frozen = compare_runs.freeze_checkpoints(
                control,
                candidate,
                checkpoints,
                compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2,
            )
            self.assertEqual(frozen["newly_frozen"], 10)
            checkpoint = checkpoints / "checkpoint-0010"
            checkpoint_meta = json.loads((checkpoint / "checkpoint.json").read_text())
            self.assertEqual(
                checkpoint_meta["parity"]["candidate_treatment"]["name"],
                compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2,
            )

            with mock.patch.object(
                compare_runs, "validate_runtime_manifest"
            ) as runtime:
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError,
                    "requires explicit --candidate-treatment",
                ):
                    compare_runs.evaluate_and_seal_batch(
                        checkpoint,
                        base / "evaluate.py",
                        base / "runtime.json",
                        base / "cache",
                    )
                runtime.assert_not_called()

            evaluator = base / "evaluate.py"
            evaluator.write_text("# pinned test evaluator\n", encoding="utf-8")
            runtime_manifest_path = base / "runtime.json"
            runtime_manifest = {
                "schema_version": 1,
                "gold_parquet": {
                    "path": str(base / "gold.parquet"),
                    "sha256": compare_runs.PINNED_GOLD_SHA256,
                },
                "contextbench_checkout": {
                    "path": str(base),
                    "commit": compare_runs.PINNED_CONTEXTBENCH_COMMIT,
                    "clean": True,
                },
                "evaluator": {
                    "path": str(evaluator),
                    "sha256": hashlib.sha256(evaluator.read_bytes()).hexdigest(),
                },
                "python": {
                    "executable": sys.executable,
                    "version": compare_runs.PINNED_PYTHON_VERSION,
                },
                "dependencies": dict(compare_runs.PINNED_DEPENDENCIES),
                "environment": {
                    "SYMBOL_DETAIL_MAX": "0",
                    "PYTHONPATH": str(base),
                },
                "invocation_template": [
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
                ],
            }
            write_json(runtime_manifest_path, runtime_manifest)

            def fake_evaluator(command, **kwargs):
                prediction_path = kwargs["prediction_path"]
                batch_ids = [
                    row["instance_id"]
                    for row in map(json.loads, prediction_path.read_text().splitlines())
                ]
                value = 0.2 if "control" in prediction_path.name else 0.3
                write_jsonl(
                    kwargs["result_path"],
                    [result(instance_id, value) for instance_id in batch_ids],
                )
                kwargs["stdout_path"].write_text("stdout\n", encoding="utf-8")
                kwargs["stderr_path"].write_text("stderr\n", encoding="utf-8")
                environment = compare_runs.evaluator_execution_environment(
                    runtime_manifest
                )
                return {
                    "command": command,
                    "cwd": str(kwargs["cwd"]),
                    "environment": environment,
                    "returncode": 0,
                    "prediction_sha256": hashlib.sha256(
                        prediction_path.read_bytes()
                    ).hexdigest(),
                    "result_sha256": hashlib.sha256(
                        kwargs["result_path"].read_bytes()
                    ).hexdigest(),
                    "stdout_sha256": hashlib.sha256(
                        kwargs["stdout_path"].read_bytes()
                    ).hexdigest(),
                    "stderr_sha256": hashlib.sha256(
                        kwargs["stderr_path"].read_bytes()
                    ).hexdigest(),
                }

            with (
                mock.patch.object(
                    compare_runs,
                    "validate_runtime_manifest",
                    return_value=runtime_manifest,
                ),
                mock.patch.object(
                    compare_runs,
                    "run_evaluator_command",
                    side_effect=fake_evaluator,
                ),
            ):
                sealed = compare_runs.evaluate_and_seal_batch(
                    checkpoint,
                    evaluator,
                    runtime_manifest_path,
                    base / "cache",
                    compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2,
                )
            self.assertEqual(
                sealed["candidate_treatment"]["name"],
                compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2,
            )
            comparison = compare_runs.checkpoint_comparison(
                control,
                checkpoints,
                10,
                candidate_treatment=(
                    compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2
                ),
            )
            self.assertEqual(comparison["cumulative"]["count"], 10)
            self.assertEqual(
                comparison["validation"]["parity"]["candidate_treatment"]["name"],
                compare_runs.CANDIDATE_TREATMENT_OFFLINE_PACKING_V2,
            )

    def test_remediation_attempt_lock_serializes_packaging_transitions(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt_root = Path(temporary) / "attempt"
            attempt_root.mkdir()
            authorization_path = attempt_root / "authorization.json"
            create_entered = threading.Event()
            release_create = threading.Event()
            predeclare_entered = threading.Event()
            call_order = []
            errors = []

            def fake_create(*_args):
                call_order.append("create-enter")
                create_entered.set()
                if not release_create.wait(5):
                    raise AssertionError("test did not release create transition")
                call_order.append("create-exit")
                return {"mode": "create"}

            def fake_predeclare(*_args):
                call_order.append("predeclare-enter")
                predeclare_entered.set()
                return {"mode": "predeclare"}

            def invoke_create():
                try:
                    compare_runs.create_remediation_bundle(
                        attempt_root,
                        attempt_root,
                        attempt_root,
                        attempt_root / "declaration.json",
                        authorization_path,
                        attempt_root / "rerun",
                        attempt_root / "evaluate.py",
                        attempt_root / "runtime.json",
                        attempt_root / "cache",
                        attempt_root / "bundle",
                    )
                except BaseException as error:
                    errors.append(error)

            def invoke_predeclare():
                try:
                    compare_runs.predeclare_packaging_retry(
                        attempt_root,
                        attempt_root,
                        attempt_root,
                        attempt_root / "declaration.json",
                        authorization_path,
                        attempt_root / "rerun",
                        attempt_root / "evaluate.py",
                        attempt_root / "runtime.json",
                        compare_runs.PACKAGING_RETRY_FAILED_PACKAGER_SHA256,
                    )
                except BaseException as error:
                    errors.append(error)

            with (
                mock.patch.object(
                    compare_runs,
                    "_create_remediation_bundle_locked",
                    side_effect=fake_create,
                ),
                mock.patch.object(
                    compare_runs,
                    "_predeclare_packaging_retry_locked",
                    side_effect=fake_predeclare,
                ),
            ):
                create_thread = threading.Thread(target=invoke_create)
                predeclare_thread = threading.Thread(target=invoke_predeclare)
                create_thread.start()
                self.assertTrue(create_entered.wait(2))
                predeclare_thread.start()
                self.assertFalse(predeclare_entered.wait(0.2))
                release_create.set()
                create_thread.join(5)
                predeclare_thread.join(5)

            self.assertFalse(create_thread.is_alive())
            self.assertFalse(predeclare_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(
                call_order,
                ["create-enter", "create-exit", "predeclare-enter"],
            )

    def test_exclusive_authorization_group_has_one_race_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = [root / f"authorization-part-{index}" for index in range(3)]

            def contender(marker):
                try:
                    compare_runs.write_exclusive_group(
                        tuple((path, marker) for path in paths)
                    )
                    return True
                except FileExistsError:
                    return False

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(contender, (b"first-writer", b"second-writer"))
                )
            self.assertEqual(results.count(True), 1)
            payloads = [path.read_bytes() for path in paths]
            self.assertEqual(len(set(payloads)), 1)
            self.assertIn(payloads[0], {b"first-writer", b"second-writer"})

    def test_full_self_compare_validates_but_fails_release_gain_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "control"
            ids = [f"task-{index:03d}" for index in range(100)]
            write_full_run(root, ids)
            output = compare_runs.full_comparison(root, root)
            self.assertEqual(output["comparison"]["delta"]["line"]["f1"], 0.0)
            self.assertFalse(output["gate"]["passed"])
            self.assertIn(
                "line_f1_delta_at_least_0_03", output["gate"]["failure_reasons"]
            )

    def test_freeze_orders_atomic_terminal_records_and_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            checkpoints = base / "checkpoints"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids)
            source_state = {
                path.relative_to(candidate): (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mtime_ns,
                )
                for path in candidate.rglob("*")
                if path.is_file()
            }

            output = compare_runs.freeze_checkpoints(control, candidate, checkpoints)
            self.assertEqual(output["newly_frozen"], 20)
            self.assertEqual(output["pending_terminal_tasks"], 5)
            self.assertEqual(len(output["new_checkpoints"]), 2)
            first = checkpoints / "checkpoint-0010"
            self.assertEqual(
                compare_runs.manifest_ids(first / "manifest.json"), ids[:10]
            )
            completion = json.loads((first / "completion-records.json").read_text())
            self.assertEqual(completion["per_instance"][ids[4]]["status"], "failure")
            self.assertEqual(
                (first / "manifest.json").read_bytes(),
                (first / "control-manifest.json").read_bytes(),
            )
            checkpoint_hash = hashlib.sha256(
                (first / "checkpoint.json").read_bytes()
            ).hexdigest()

            repeated = compare_runs.freeze_checkpoints(control, candidate, checkpoints)
            self.assertEqual(repeated["newly_frozen"], 0)
            self.assertEqual(
                hashlib.sha256((first / "checkpoint.json").read_bytes()).hexdigest(),
                checkpoint_hash,
            )
            after_state = {
                path.relative_to(candidate): (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mtime_ns,
                )
                for path in candidate.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after_state, source_state)

    def test_remediation_binding_uses_frozen_control_triage_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            checkpoints = base / "checkpoints"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids, count=10)
            compare_runs.freeze_checkpoints(control, candidate, checkpoints)

            checkpoint = compare_runs.load_checkpoint_chain(checkpoints)[0]
            checkpoint_dir = checkpoint["dir"]
            evaluation = checkpoint_dir / "evaluation"
            for name in (
                "batch-evaluation.json",
                "candidate-results.jsonl",
                "control-results.jsonl",
                "runtime-manifest.json",
            ):
                (evaluation / name).parent.mkdir(parents=True, exist_ok=True)
                (evaluation / name).write_text(f"{name}\n", encoding="utf-8")

            self.assertTrue((checkpoint_dir / "control-triage.jsonl").is_file())
            self.assertFalse((checkpoint_dir / "triage").exists())
            with mock.patch.object(
                compare_runs,
                "load_sealed_batch",
                return_value={
                    "evaluator_sha256": "e" * 64,
                    "runtime_manifest_sha256": "r" * 64,
                },
            ):
                binding = compare_runs.remediation_checkpoint_binding_v2(checkpoint)

            self.assertEqual(
                binding["triage_sha256"],
                checkpoint["meta"]["frozen_files"]["control-triage.jsonl"],
            )

    def test_freeze_rejects_malformed_status(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            checkpoints = base / "checkpoints"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids)
            first_record = candidate / "runs" / ids[0] / "run_record.json"
            record = json.loads(first_record.read_text())
            original_mtime = first_record.stat().st_mtime_ns
            record["status"] = "running"
            write_json(first_record, record)
            os.utime(first_record, ns=(original_mtime, original_mtime))
            with self.assertRaisesRegex(compare_runs.ArtifactError, "malformed status"):
                compare_runs.freeze_checkpoints(control, candidate, checkpoints)

    def test_freeze_rejects_tampered_failure_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids, count=10)
            failure_path = candidate / "runs" / ids[4] / "failure.json"
            failure = json.loads(failure_path.read_text())
            failure["message"] = "tampered after terminal"
            write_json(failure_path, failure)

            with self.assertRaisesRegex(
                compare_runs.ArtifactError, "failure artifact hash"
            ):
                compare_runs.freeze_checkpoints(
                    control, candidate, base / "checkpoints"
                )

    def test_freeze_attests_exact_legacy_failure_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids, count=10)
            make_legacy_nonzero_failure(candidate, ids[4])

            output = compare_runs.freeze_checkpoints(
                control, candidate, base / "checkpoints"
            )

            self.assertEqual(output["legacy_failure_attestation_ids"], [ids[4]])
            checkpoint = base / "checkpoints/checkpoint-0010"
            completion = json.loads(
                (checkpoint / "completion-records.json").read_text()
            )
            record = completion["per_instance"][ids[4]]
            self.assertEqual(
                record["validation_mode"],
                "legacy_failure_semantic_attestation_v1",
            )
            attestation = record["legacy_failure_attestation"]
            self.assertTrue(attestation["exact_zero_prediction_stub_verified"])
            self.assertTrue(attestation["audit_exactly_matches_failure"])
            self.assertTrue(attestation["query_plan_absent"])
            self.assertEqual(attestation["frozen_audit_sha256"], record["audit_sha256"])
            self.assertEqual(
                attestation["frozen_failure_sha256"], record["failure_sha256"]
            )
            compare_runs.load_checkpoint_chain(base / "checkpoints")

    def test_freeze_rejects_unsafe_legacy_failure_variants(self):
        variants = (
            "partial_hash",
            "query_plan",
            "nonzero_prediction",
            "audit_mismatch",
        )
        for variant in variants:
            with (
                self.subTest(variant=variant),
                tempfile.TemporaryDirectory() as directory,
            ):
                base = Path(directory).resolve()
                control = base / "control"
                candidate = base / "candidate"
                ids = [f"task-{index:03d}" for index in range(100)]
                manifest_bytes = write_full_run(control, ids)
                write_partial_candidate(candidate, manifest_bytes, ids, count=10)
                instance_id = ids[4]
                make_legacy_nonzero_failure(candidate, instance_id)
                run_dir = candidate / "runs" / instance_id
                record_path = run_dir / "run_record.json"
                record_mtime = record_path.stat().st_mtime_ns
                record = json.loads(record_path.read_text())
                if variant == "partial_hash":
                    record["audit_sha256"] = hashlib.sha256(
                        (
                            run_dir / "prediction-audit" / f"{instance_id}.json"
                        ).read_bytes()
                    ).hexdigest()
                    write_json(record_path, record)
                elif variant == "query_plan":
                    write_json(run_dir / "query-plan.json", {instance_id: ["unbound"]})
                elif variant == "nonzero_prediction":
                    prediction_path = run_dir / "prediction.jsonl"
                    row = json.loads(prediction_path.read_text())
                    row["traj_data"]["pred_steps"] = [{"files": ["wrong.py"]}]
                    write_jsonl(prediction_path, [row])
                    record["prediction_sha256"] = hashlib.sha256(
                        prediction_path.read_bytes()
                    ).hexdigest()
                    write_json(record_path, record)
                else:
                    audit_path = run_dir / "prediction-audit" / f"{instance_id}.json"
                    audit = json.loads(audit_path.read_text())
                    audit["harness_failure"]["message"] = "changed"
                    write_json(audit_path, audit)
                os.utime(record_path, ns=(record_mtime, record_mtime))
                with self.assertRaises(compare_runs.ArtifactError):
                    compare_runs.freeze_checkpoints(
                        control, candidate, base / "checkpoints"
                    )

    def test_frozen_failure_remains_authoritative_after_resume_overwrites_source(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            resumed = base / "candidate-resumed"
            checkpoints = base / "checkpoints"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids, count=10)
            compare_runs.freeze_checkpoints(control, candidate, checkpoints)

            shutil.copytree(candidate, resumed)
            write_candidate_task(
                resumed,
                ids[4],
                4,
                failed=False,
                mtime_second=50,
            )
            for index in range(10, 20):
                write_candidate_task(resumed, ids[index], index)

            output = compare_runs.freeze_checkpoints(control, resumed, checkpoints)

            self.assertEqual(output["newly_frozen"], 10)
            self.assertEqual(
                output["new_checkpoints"],
                [str(checkpoints / "checkpoint-0020")],
            )
            drift = output["source_reconciliation"]["current_source_drift"]
            self.assertEqual([event["instance_id"] for event in drift], [ids[4]])
            self.assertEqual(drift[0]["kind"], "source_terminal_changed_after_freeze")
            latest = checkpoints / "checkpoint-0020"
            completion = json.loads((latest / "completion-records.json").read_text())
            self.assertEqual(completion["per_instance"][ids[4]]["status"], "failure")
            frozen_predictions = {
                row["instance_id"]: row
                for row in map(
                    json.loads,
                    (latest / "candidate-predictions.jsonl").read_text().splitlines(),
                )
            }
            self.assertEqual(
                frozen_predictions[ids[4]]["harness_failure"]["kind"], "timeout"
            )
            live_prediction = json.loads(
                (resumed / "runs" / ids[4] / "prediction.jsonl").read_text()
            )
            self.assertNotIn("harness_failure", live_prediction)
            reconciliation = json.loads(
                (latest / "source-reconciliation.json").read_text()
            )
            self.assertTrue(reconciliation["frozen_completion_archive_authoritative"])
            self.assertEqual(len(reconciliation["events"]), 1)

    def test_checkpoint_root_rejects_a_different_candidate_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            checkpoints = base / "checkpoints"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids)
            compare_runs.freeze_checkpoints(control, candidate, checkpoints)
            provenance = json.loads((candidate / "run_provenance.json").read_text())
            provenance["fingerprint"] = "fp-other-candidate"
            write_json(candidate / "run_provenance.json", provenance)
            for instance_id in ids[:25]:
                run_dir = candidate / "runs" / instance_id
                record_path = run_dir / "run_record.json"
                mtime = record_path.stat().st_mtime_ns
                record = json.loads(record_path.read_text())
                record["run_fingerprint"] = "fp-other-candidate"
                if record["status"] == "failure":
                    failure_path = run_dir / "failure.json"
                    failure = json.loads(failure_path.read_text())
                    failure["run_fingerprint"] = "fp-other-candidate"
                    write_json(failure_path, failure)
                    audit_path = run_dir / "prediction-audit" / f"{instance_id}.json"
                    write_json(audit_path, {"harness_failure": failure})
                    prediction_path = run_dir / "prediction.jsonl"
                    row = json.loads(prediction_path.read_text())
                    row["harness_failure"]["run_fingerprint"] = "fp-other-candidate"
                    write_jsonl(prediction_path, [row])
                    record["prediction_sha256"] = hashlib.sha256(
                        prediction_path.read_bytes()
                    ).hexdigest()
                    record["audit_sha256"] = hashlib.sha256(
                        audit_path.read_bytes()
                    ).hexdigest()
                    record["failure_sha256"] = hashlib.sha256(
                        failure_path.read_bytes()
                    ).hexdigest()
                write_json(record_path, record)
                os.utime(record_path, ns=(mtime, mtime))
            with self.assertRaisesRegex(
                compare_runs.ArtifactError, "mixes candidate run fingerprints"
            ):
                compare_runs.freeze_checkpoints(control, candidate, checkpoints)

    def test_freeze_rejects_same_second_terminal_order_ambiguity(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids)
            first = candidate / "runs" / ids[0] / "run_record.json"
            second = candidate / "runs" / ids[1] / "run_record.json"
            os.utime(second, ns=(first.stat().st_mtime_ns, first.stat().st_mtime_ns))
            with self.assertRaisesRegex(
                compare_runs.ArtifactError, "ordering is ambiguous"
            ):
                compare_runs.freeze_checkpoints(
                    control, candidate, base / "checkpoints"
                )

    def test_sealed_batches_accumulate_batch_and_cumulative_comparisons(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            checkpoints = base / "checkpoints"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids)
            compare_runs.freeze_checkpoints(control, candidate, checkpoints)
            checkout = base / "contextbench"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--no-hardlinks",
                    "/private/tmp/contextbench",
                    str(checkout),
                ],
                check=True,
            )
            evaluator = checkout / "contextbench/evaluate.py"
            runtime_manifest = base / "runtime-manifest.json"
            write_runtime_manifest(runtime_manifest, evaluator, checkout)

            def fake_evaluator(command, **kwargs):
                prediction_path = kwargs["prediction_path"]
                ids_for_run = [
                    row["instance_id"]
                    for row in map(json.loads, prediction_path.read_text().splitlines())
                ]
                value = 0.2 if "control" in prediction_path.name else 0.3
                write_jsonl(
                    kwargs["result_path"],
                    [result(instance_id, value) for instance_id in ids_for_run],
                )
                kwargs["stdout_path"].write_text("stdout\n", encoding="utf-8")
                kwargs["stderr_path"].write_text("stderr\n", encoding="utf-8")
                return {
                    "command": command,
                    "cwd": str(kwargs["cwd"]),
                    "environment": {
                        "PYTHONPATH": kwargs["environment"]["PYTHONPATH"],
                        "SYMBOL_DETAIL_MAX": kwargs["environment"]["SYMBOL_DETAIL_MAX"],
                        "PYTHONDONTWRITEBYTECODE": kwargs["environment"][
                            "PYTHONDONTWRITEBYTECODE"
                        ],
                        "PYTHONNOUSERSITE": kwargs["environment"]["PYTHONNOUSERSITE"],
                    },
                    "returncode": 0,
                    "prediction_sha256": hashlib.sha256(
                        prediction_path.read_bytes()
                    ).hexdigest(),
                    "result_sha256": hashlib.sha256(
                        kwargs["result_path"].read_bytes()
                    ).hexdigest(),
                    "stdout_sha256": hashlib.sha256(
                        kwargs["stdout_path"].read_bytes()
                    ).hexdigest(),
                    "stderr_sha256": hashlib.sha256(
                        kwargs["stderr_path"].read_bytes()
                    ).hexdigest(),
                }

            def mismatched_control(command, **kwargs):
                receipt = fake_evaluator(command, **kwargs)
                if "control" in kwargs["prediction_path"].name:
                    ids_for_run = [
                        row["instance_id"]
                        for row in map(
                            json.loads,
                            kwargs["prediction_path"].read_text().splitlines(),
                        )
                    ]
                    write_jsonl(
                        kwargs["result_path"],
                        [result(instance_id, 0.21) for instance_id in ids_for_run],
                    )
                    receipt["result_sha256"] = hashlib.sha256(
                        kwargs["result_path"].read_bytes()
                    ).hexdigest()
                return receipt

            with mock.patch.object(
                compare_runs, "run_evaluator_command", side_effect=mismatched_control
            ):
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError, "differs from stored official result"
                ):
                    compare_runs.evaluate_and_seal_batch(
                        checkpoints / "checkpoint-0010",
                        evaluator,
                        runtime_manifest,
                        base / "eval-cache",
                    )
            self.assertFalse((checkpoints / "checkpoint-0010" / "evaluation").exists())

            with mock.patch.object(
                compare_runs, "run_evaluator_command", side_effect=fake_evaluator
            ):
                for count in (10, 20):
                    checkpoint = checkpoints / f"checkpoint-{count:04d}"
                    compare_runs.evaluate_and_seal_batch(
                        checkpoint,
                        evaluator,
                        runtime_manifest,
                        base / "eval-cache",
                    )
            with mock.patch.object(
                compare_runs, "run_evaluator_command"
            ) as evaluator_call:
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError, "refusing to replace"
                ):
                    compare_runs.evaluate_and_seal_batch(
                        checkpoints / "checkpoint-0010",
                        evaluator,
                        runtime_manifest,
                        base / "eval-cache",
                    )
                evaluator_call.assert_not_called()

            output = compare_runs.checkpoint_comparison(control, checkpoints, 20)
            self.assertEqual(output["batch"]["count"], 10)
            self.assertEqual(output["cumulative"]["count"], 20)
            self.assertAlmostEqual(output["batch"]["delta"]["line"]["f1"], 0.1)
            # One of the 20 candidate completions is a zero-accounted timeout.
            self.assertAlmostEqual(output["cumulative"]["delta"]["line"]["f1"], 0.085)
            self.assertTrue(output["gate"]["provisional"])
            self.assertFalse(output["gate"]["passed"])
            self.assertIn(
                "cumulative:failure_count_increased", output["gate"]["failure_reasons"]
            )
            self.assertEqual(len(output["sealed_batches"]), 2)
            tampered_log = (
                checkpoints
                / "checkpoint-0020"
                / "evaluation"
                / "candidate-evaluator.stderr.log"
            )
            tampered_log.write_text("fabricated\n", encoding="utf-8")
            with self.assertRaisesRegex(
                compare_runs.ArtifactError, "sealed evaluator artifact changed"
            ):
                compare_runs.checkpoint_comparison(control, checkpoints, 20)

    def test_seal_ignores_an_inherited_dangling_contextbench_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            candidate = base / "candidate"
            checkpoints = base / "checkpoints"
            ids = [f"task-{index:03d}" for index in range(100)]
            manifest_bytes = write_full_run(control, ids)
            write_partial_candidate(candidate, manifest_bytes, ids)
            compare_runs.freeze_checkpoints(control, candidate, checkpoints)

            checkout = base / "contextbench"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--no-hardlinks",
                    "/private/tmp/contextbench",
                    str(checkout),
                ],
                check=True,
            )
            evaluator = checkout / "contextbench/evaluate.py"
            runtime_manifest = base / "runtime-manifest.json"
            write_runtime_manifest(runtime_manifest, evaluator, checkout)

            repo_spec = importlib.util.spec_from_file_location(
                "contextbench_repo_for_isolation_test",
                checkout / "contextbench/core/repo.py",
            )
            assert repo_spec and repo_spec.loader
            repo_module = importlib.util.module_from_spec(repo_spec)
            repo_spec.loader.exec_module(repo_module)

            source_repo = base / "source-repo"
            source_repo.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=source_repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "contextbench-test@example.invalid"],
                cwd=source_repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "ContextBench test"],
                cwd=source_repo,
                check=True,
            )
            (source_repo / "tracked.txt").write_text("content\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=source_repo, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "test fixture"],
                cwd=source_repo,
                check=True,
            )
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source_repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            inherited_root = base / "inherited-contextbench-tmp"
            dangling_worktree = (
                inherited_root
                / "contextbench_worktrees"
                / repo_module._normalize_url(str(source_repo))
                / commit
            )
            dangling_worktree.mkdir(parents=True)
            (dangling_worktree / ".git").write_text(
                "gitdir: /missing/contextbench/cache/worktree\n",
                encoding="utf-8",
            )
            cache = base / "eval-cache"
            with mock.patch.dict(
                os.environ,
                {"CONTEXTBENCH_TMP_ROOT": str(inherited_root)},
            ):
                self.assertIsNone(
                    repo_module.checkout(
                        str(source_repo),
                        commit,
                        str(cache),
                        verbose=False,
                    )
                )

                observed_roots = []

                def fake_evaluator(command, **kwargs):
                    prediction_path = kwargs["prediction_path"]
                    isolated_root = Path(kwargs["environment"]["CONTEXTBENCH_TMP_ROOT"])
                    observed_roots.append(isolated_root)
                    self.assertNotEqual(isolated_root, inherited_root)
                    self.assertTrue(isolated_root.is_dir())
                    with mock.patch.dict(
                        os.environ,
                        {"CONTEXTBENCH_TMP_ROOT": str(isolated_root)},
                    ):
                        repo_dir = repo_module.checkout(
                            str(source_repo),
                            commit,
                            str(cache),
                            verbose=False,
                        )
                    self.assertIsNotNone(repo_dir)
                    value = 0.2 if "control" in prediction_path.name else 0.3
                    ids_for_run = [
                        row["instance_id"]
                        for row in map(
                            json.loads,
                            prediction_path.read_text().splitlines(),
                        )
                    ]
                    write_jsonl(
                        kwargs["result_path"],
                        [result(instance_id, value) for instance_id in ids_for_run],
                    )
                    kwargs["stdout_path"].write_text("stdout\n", encoding="utf-8")
                    kwargs["stderr_path"].write_text("stderr\n", encoding="utf-8")
                    return {
                        "command": command,
                        "cwd": str(kwargs["cwd"]),
                        "environment": {
                            key: kwargs["environment"][key]
                            for key in (
                                "PYTHONPATH",
                                "SYMBOL_DETAIL_MAX",
                                "PYTHONDONTWRITEBYTECODE",
                                "PYTHONNOUSERSITE",
                            )
                        },
                        "returncode": 0,
                        "prediction_sha256": hashlib.sha256(
                            prediction_path.read_bytes()
                        ).hexdigest(),
                        "result_sha256": hashlib.sha256(
                            kwargs["result_path"].read_bytes()
                        ).hexdigest(),
                        "stdout_sha256": hashlib.sha256(
                            kwargs["stdout_path"].read_bytes()
                        ).hexdigest(),
                        "stderr_sha256": hashlib.sha256(
                            kwargs["stderr_path"].read_bytes()
                        ).hexdigest(),
                    }

                with mock.patch.object(
                    compare_runs,
                    "run_evaluator_command",
                    side_effect=fake_evaluator,
                ):
                    compare_runs.evaluate_and_seal_batch(
                        checkpoints / "checkpoint-0010",
                        evaluator,
                        runtime_manifest,
                        cache,
                    )

            self.assertEqual(len(observed_roots), 2)
            self.assertEqual(observed_roots[0], observed_roots[1])
            self.assertFalse(observed_roots[0].exists())
            self.assertTrue(dangling_worktree.exists())
            seal = json.loads(
                (
                    checkpoints / "checkpoint-0010/evaluation/batch-evaluation.json"
                ).read_text()
            )
            self.assertEqual(
                seal["execution_receipt"]["worktree_isolation"],
                compare_runs.PAIRED_EVALUATOR_WORKTREE_ISOLATION,
            )
            self.assertNotIn(str(observed_roots[0]), json.dumps(seal))

    def test_provisional_gate_rejects_evaluator_error_and_latest_batch_regression(self):
        positive = {
            "delta": {
                granularity: {"f1": 0.1} for granularity in compare_runs.GRANULARITIES
            },
            "f1_bootstrap": {
                granularity: {"ci_95": {"lower": 0.01, "upper": 0.2}}
                for granularity in compare_runs.GRANULARITIES
            },
            "line_f1_bootstrap": {"ci_95": {"lower": 0.01, "upper": 0.2}},
        }
        negative_batch = json.loads(json.dumps(positive))
        negative_batch["f1_bootstrap"]["file"]["ci_95"] = {
            "lower": -0.2,
            "upper": -0.01,
        }
        clean = {"failures": 0, "timeouts": 0, "evaluator_errors": 0}
        evaluator_error = {"failures": 0, "timeouts": 0, "evaluator_errors": 1}
        cumulative_gate = compare_runs.provisional_gate(positive, {}, clean, clean)
        error_gate = compare_runs.provisional_gate(positive, {}, clean, evaluator_error)
        batch_gate = compare_runs.provisional_gate(negative_batch, {}, clean, clean)
        self.assertTrue(cumulative_gate["passed"])
        self.assertFalse(error_gate["passed"])
        self.assertIn("evaluator_error_count_increased", error_gate["failure_reasons"])
        self.assertFalse(batch_gate["passed"])
        self.assertIn(
            "paired_file_f1_ci_upper_below_zero", batch_gate["failure_reasons"]
        )
        self.assertFalse(cumulative_gate["passed"] and batch_gate["passed"])

    def test_n_100_release_eligibility_requires_every_gate_and_reconciliation(self):
        passing = {"passed": True, "checks": {"ok": True}, "failure_reasons": []}
        failing = {
            "passed": False,
            "checks": {"ok": False},
            "failure_reasons": ["regression"],
        }
        failed_gate = compare_runs.compose_checkpoint_gate(
            100,
            json.loads(json.dumps(passing)),
            json.loads(json.dumps(failing)),
            {"complete": True},
        )
        self.assertFalse(failed_gate["release_eligible"])
        self.assertFalse(failed_gate["cumulative_gate"]["release_eligible"])

        unreconciled = compare_runs.compose_checkpoint_gate(
            100,
            json.loads(json.dumps(passing)),
            json.loads(json.dumps(passing)),
            None,
        )
        self.assertFalse(unreconciled["release_eligible"])
        self.assertFalse(unreconciled["cumulative_gate"]["release_eligible"])

        eligible = compare_runs.compose_checkpoint_gate(
            100,
            json.loads(json.dumps(passing)),
            json.loads(json.dumps(passing)),
            {"complete": True},
        )
        self.assertTrue(eligible["release_eligible"])
        self.assertTrue(eligible["cumulative_gate"]["release_eligible"])

    def test_latest_paths_are_rejected(self):
        with self.assertRaisesRegex(compare_runs.ArtifactError, "LATEST"):
            compare_runs.explicit_path("/tmp/LATEST/run", "candidate", must_exist=False)

    def test_runtime_manifest_rejects_unclean_checkout_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "runtime.json"
            evaluator = Path("/private/tmp/contextbench/contextbench/evaluate.py")
            write_runtime_manifest(path, evaluator)
            manifest = json.loads(path.read_text())
            manifest["contextbench_checkout"]["clean"] = False
            write_json(path, manifest)
            with self.assertRaisesRegex(
                compare_runs.ArtifactError, "clean ContextBench"
            ):
                compare_runs.validate_runtime_manifest(path, evaluator)

    def test_runtime_manifest_rejects_untracked_pythonpath_code(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            checkout = base / "contextbench"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--no-hardlinks",
                    "/private/tmp/contextbench",
                    str(checkout),
                ],
                check=True,
            )
            evaluator = checkout / "contextbench/evaluate.py"
            path = base / "runtime.json"
            write_runtime_manifest(path, evaluator, checkout)
            (checkout / "sitecustomize.py").write_text(
                "MEMTRACE_UNTRACKED_RUNTIME_TEST = True\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                compare_runs.ArtifactError,
                "tracked or untracked modifications",
            ):
                compare_runs.validate_runtime_manifest(path, evaluator)

    def test_runtime_manifest_rejects_ignored_python_bytecode(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            checkout = base / "contextbench"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--no-hardlinks",
                    "/private/tmp/contextbench",
                    str(checkout),
                ],
                check=True,
            )
            evaluator = checkout / "contextbench/evaluate.py"
            path = base / "runtime.json"
            write_runtime_manifest(path, evaluator, checkout)
            (checkout / "sitecustomize.pyc").write_bytes(b"ignored executable bytecode")
            dirty = subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(dirty, "")
            with self.assertRaisesRegex(
                compare_runs.ArtifactError,
                "ignored import-affecting Python artifacts",
            ):
                compare_runs.validate_runtime_manifest(path, evaluator)

    def test_n_100_cannot_be_release_eligible_without_complete_candidate_run(self):
        with self.assertRaisesRegex(
            compare_runs.ArtifactError, "requires --candidate-run"
        ):
            compare_runs.checkpoint_comparison(Path("/unused"), Path("/unused"), 100)

    def test_false_kill_attestation_requires_one_impossible_watchdog_elapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "checkpoint-0050"
            target = "task-false-kill"
            run_relative = f"runs/{target}/run_record.json"
            failure_relative = f"runs/{target}/failure.json"
            run_path = root / "source-artifacts" / run_relative
            failure_path = root / "source-artifacts" / failure_relative
            write_json(run_path, {"instance_id": target, "status": "failure"})
            write_json(
                failure_path,
                {
                    "instance_id": target,
                    "returncode": -9,
                    "seconds": 0.4,
                },
            )
            record = {
                "status": "failure",
                "validation_mode": "legacy_failure_semantic_attestation_v1",
                "run_record_relative_path": run_relative,
                "failure_relative_path": failure_relative,
                "run_record_sha256": hashlib.sha256(run_path.read_bytes()).hexdigest(),
                "prediction_sha256": "p" * 64,
                "audit_sha256": "a" * 64,
                "query_plan_sha256": None,
                "failure_sha256": hashlib.sha256(failure_path.read_bytes()).hexdigest(),
            }
            record["legacy_failure_attestation"] = {
                "exact_zero_prediction_stub_verified": True,
                "audit_exactly_matches_failure": True,
                "query_plan_absent": True,
                "frozen_audit_sha256": record["audit_sha256"],
                "frozen_failure_sha256": record["failure_sha256"],
                "failure_returncode": -9,
            }
            write_json(
                root / "completion-records.json",
                {"per_instance": {target: record}},
            )
            write_json(root / "checkpoint.json", {"schema_version": 1})
            impossible = (
                f"outcome=timeout slug={target} pid=1 elapsed_s=4123168608 "
                "limit_s=5400\n"
            )
            (root / "watchdog.log").write_text(impossible, encoding="utf-8")
            checkpoint = {"dir": root, "ids": [target] * 50, "batch_ids": [target]}

            attestation = compare_runs.false_kill_attestation(checkpoint, target)
            self.assertEqual(
                attestation["mode"],
                "impossible_watchdog_elapsed_false_kill_v1",
            )
            self.assertEqual(attestation["recorded_runner_seconds"], 0.4)

            (root / "watchdog.log").write_text(
                f"outcome=timeout slug={target} elapsed_s=10 limit_s=5\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                compare_runs.ArtifactError, "impossible elapsed"
            ):
                compare_runs.false_kill_attestation(checkpoint, target)

            (root / "watchdog.log").write_text(
                impossible + impossible, encoding="utf-8"
            )
            with self.assertRaisesRegex(
                compare_runs.ArtifactError, "one exact watchdog"
            ):
                compare_runs.false_kill_attestation(checkpoint, target)

    def test_post_n_100_prepare_and_authorize_are_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control = base / "control"
            checkpoints = base / "checkpoints"
            candidate = base / "candidate"
            latest = checkpoints / "checkpoint-0100"
            origin = checkpoints / "checkpoint-0050"
            for path in (control, candidate, latest, origin):
                path.mkdir(parents=True)
            target = "task-remediate"
            ids = [f"task-{index:03d}" for index in range(100)]
            ids[45] = target
            write_json(latest / "manifest.json", ids)
            write_json(latest / "checkpoint.json", {"schema_version": 1})
            write_json(origin / "batch-manifest.json", ids[40:50])
            write_json(origin / "checkpoint.json", {"schema_version": 1, "count": 50})
            raw_meta = {
                "schema_version": 1,
                "run_id": "raw-run",
                "concurrency": 12,
                "manifest_instances": 100,
                "core_pinning": {"enabled": True, "slots": 12},
                "memtrace_source": {
                    "head_sha": "h" * 40,
                    "binary_sha256": "b" * 64,
                },
            }
            raw_provenance = json.loads(json.dumps(REAL_N90_PROVENANCE))
            write_json(latest / "run_meta.json", raw_meta)
            write_json(latest / "run_provenance.json", raw_provenance)
            self.assertEqual(
                hashlib.sha256(
                    (latest / "run_provenance.json").read_bytes()
                ).hexdigest(),
                "f4a0cd173574e1db19a1013356f421e3a355fdd715aedb7158919584ffceb735",
            )
            declaration_core = {
                "schema_version": 1,
                "mode": "predeclared-false-kill-remediation",
                "target_ids": [target],
            }
            declaration = {
                **declaration_core,
                "declaration_sha256": compare_runs.canonical_sha256(declaration_core),
            }
            declaration_path = base / "predeclaration.json"
            write_json(declaration_path, declaration)
            manifest_path = base / "one-task.json"
            write_json(manifest_path, [target])
            state = {
                "latest": {"dir": latest, "ids": ids, "batch_ids": ids[-10:]},
                "chain": [
                    {"dir": origin, "ids": ids[:50], "batch_ids": ids[40:50]},
                    {"dir": latest, "ids": ids, "batch_ids": ids[-10:]},
                ],
            }
            with (
                mock.patch.object(
                    compare_runs, "verify_false_kill_declaration", return_value=target
                ),
                mock.patch.object(
                    compare_runs, "load_n_100_remediation_state", return_value=state
                ),
            ):
                prepared = compare_runs.prepare_remediation_authorization(
                    control,
                    checkpoints,
                    candidate,
                    declaration_path,
                    manifest_path,
                    "rerun-1",
                    "nonce" * 8,
                )
                token_path = Path(prepared["authorization_token"])
                token = json.loads(token_path.read_text())
                self.assertEqual(
                    token["raw_n_100"]["policy_semantics"]["mode"],
                    "attested-legacy-driver-policy-v1",
                )
                self.assertEqual(
                    token["raw_n_100"]["policy_semantics"]["post_selector_policy"],
                    "off",
                )
                prospective = json.loads(json.dumps(raw_provenance))
                prospective["manifest"] = {
                    "path": "/remote/one-task.json",
                    "bytes": token["rerun"]["manifest_bytes"],
                    "sha256": token["rerun"]["manifest_sha256"],
                }
                prospective["policy"]["concurrency"] = 1
                prospective["behavior_environment"]["MEMTRACE_PIN_SLOTS"] = "1"
                prospective["memtrace"]["explicit_provenance"] = {
                    "path": "/remote/authorization-token.json",
                    "exists": True,
                    "kind": "file",
                    "bytes": token_path.stat().st_size,
                    "sha256": hashlib.sha256(token_path.read_bytes()).hexdigest(),
                }
                prospective["fingerprint"] = compare_runs.canonical_sha256(
                    compare_runs._drop_keys(prospective, {"fingerprint"})
                )
                prospective_path = base / "prospective.json"
                write_json(prospective_path, prospective)
                authorized = compare_runs.authorize_remediation_execution(
                    control,
                    checkpoints,
                    candidate,
                    declaration_path,
                    token_path,
                    prospective_path,
                )
                self.assertEqual(
                    authorized["rerun_fingerprint"], prospective["fingerprint"]
                )
                self.assertEqual(
                    compare_runs._normalized_remediation_provenance(raw_provenance),
                    compare_runs._normalized_remediation_provenance(prospective),
                )
                parity_rerun = base / "parity-rerun"
                parity_rerun.mkdir()
                parity_rerun_meta = json.loads(json.dumps(raw_meta))
                parity_rerun_meta.update(
                    {
                        "run_id": "rerun-1",
                        "concurrency": 1,
                        "manifest_instances": 1,
                    }
                )
                parity_rerun_meta["core_pinning"]["slots"] = 1
                write_json(parity_rerun / "run_meta.json", parity_rerun_meta)
                write_json(parity_rerun / "run_provenance.json", prospective)
                (parity_rerun / "manifest.json").write_bytes(manifest_path.read_bytes())
                parity = compare_runs.validate_remediation_parity(latest, parity_rerun)
                self.assertEqual(
                    parity["recorded_resource_identity"]["scope"],
                    "parity is limited to recorded metadata; exact physical-host "
                    "equivalence is not asserted",
                )
                authorization = json.loads(
                    Path(authorized["authorization"]).read_text()
                )
                self.assertEqual(
                    authorization["binding"]["raw_n_100_checkpoint_sha256"],
                    hashlib.sha256(
                        (latest / "checkpoint.json").read_bytes()
                    ).hexdigest(),
                )
                self.assertEqual(authorization["binding"]["nonce"], "nonce" * 8)
                self.assertEqual(
                    authorization["rerun_fingerprint"],
                    prospective["fingerprint"],
                )
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError, "attempt already exists"
                ):
                    compare_runs.prepare_remediation_authorization(
                        control,
                        checkpoints,
                        candidate,
                        declaration_path,
                        manifest_path,
                        "rerun-1",
                        "nonce" * 8,
                    )
                copied_declaration = base / "copied" / "predeclaration.json"
                copied_declaration.parent.mkdir()
                copied_declaration.write_bytes(declaration_path.read_bytes())
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError, "attempt already exists"
                ):
                    compare_runs.prepare_remediation_authorization(
                        control,
                        checkpoints,
                        candidate,
                        copied_declaration,
                        manifest_path,
                        "rerun-2",
                        "secondnonce" * 4,
                    )
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError, "already authorized"
                ):
                    compare_runs.authorize_remediation_execution(
                        control,
                        checkpoints,
                        candidate,
                        declaration_path,
                        token_path,
                        prospective_path,
                    )

    def test_remediation_release_gate_includes_repaired_originating_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            watchdog = Path(directory) / "watchdog.log"
            watchdog.write_text("", encoding="utf-8")
            ids = [f"task-{index:03d}" for index in range(100)]
            origin_ids = ids[40:50]
            latest_ids = ids[-10:]
            predictions = {instance_id: prediction(instance_id) for instance_id in ids}
            control_results = {
                instance_id: result(instance_id, 0.2) for instance_id in ids
            }
            candidate_results = {
                instance_id: result(
                    instance_id, 0.0 if instance_id in origin_ids else 0.3
                )
                for instance_id in ids
            }
            records = {
                instance_id: {"status": "success", "seconds": 1.0}
                for instance_id in ids
            }
            languages = {instance_id: "python" for instance_id in ids}
            control = compare_runs.synthetic_run(
                ids,
                predictions,
                control_results,
                languages,
                records,
                watchdog,
            )
            candidate = compare_runs.synthetic_run(
                ids,
                predictions,
                candidate_results,
                languages,
                records,
                watchdog,
            )
            assessment = compare_runs.recompute_remediation_assessment(
                ids,
                origin_ids,
                latest_ids,
                control,
                candidate,
                candidate,
                watchdog,
                watchdog,
                watchdog,
            )
            self.assertTrue(assessment["gate"]["cumulative_gate"]["passed"])
            self.assertTrue(assessment["gate"]["latest_batch_gate"]["passed"])
            self.assertFalse(
                assessment["gate"]["repaired_originating_batch_gate"]["passed"]
            )
            self.assertFalse(assessment["gate"]["release_eligible"])
            self.assertTrue(
                any(
                    reason.startswith("repaired_originating_batch_10:")
                    for reason in assessment["gate"]["failure_reasons"]
                )
            )

    def test_remediation_snapshot_requires_exact_attested_legacy_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            target = "task-remediate"
            manifest = base / "manifest.json"
            write_json(manifest, [target])
            dataset = base / "dataset.parquet"
            dataset.write_bytes(b"dataset fixture\n")
            raw_provenance = base / "raw-run-provenance.json"
            write_json(raw_provenance, REAL_N90_PROVENANCE)
            raw_value, raw_binding = compare_runs.remediation_raw_provenance_binding(
                raw_provenance
            )
            self.assertEqual(
                raw_binding["file_sha256"],
                "f4a0cd173574e1db19a1013356f421e3a355fdd715aedb7158919584ffceb735",
            )
            self.assertEqual(raw_binding["bytes"], 3276)
            self.assertEqual(
                raw_binding["policy_semantics"],
                {
                    "mode": "attested-legacy-driver-policy-v1",
                    "driver_sha256": (
                        "22f2c88f3f5a58ff3312830e9ec41f43be5780d207c780ece93bb99f0c92a1b3"
                    ),
                    "runner_sha256": (
                        "209c88e2517b250b011abfb7c04dba3224e936e6708d1d988ed9ebe358942658"
                    ),
                    "selector_policy_surface": "absent",
                    "post_selector_policy": "off",
                },
            )
            manifest_bytes = manifest.read_bytes()
            token_core = {
                "schema_version": 1,
                "mode": compare_runs.REMEDIATION_TOKEN_MODE,
                "nonce": "n" * 32,
                "target_id": target,
                "declaration": {
                    "declaration_sha256": "d" * 64,
                    "file_sha256": "e" * 64,
                },
                "raw_n_100": {
                    "checkpoint_sha256": "c" * 64,
                    "candidate_run_id": "raw-run",
                    "candidate_fingerprint": raw_value["fingerprint"],
                    "manifest_sha256": "m" * 64,
                    "run_provenance_sha256": raw_binding["file_sha256"],
                    "run_provenance_bytes": raw_binding["bytes"],
                    "policy_semantics": raw_binding["policy_semantics"],
                },
                "originating_batch": {
                    "count": 50,
                    "checkpoint_sha256": "o" * 64,
                    "batch_manifest_sha256": "b" * 64,
                },
                "rerun": {
                    "run_id": "rerun-1",
                    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                    "manifest_bytes": len(manifest_bytes),
                },
                "canonical_paths": {},
                "protocol": {},
            }
            token = {
                **token_core,
                "token_sha256": compare_runs.canonical_sha256(token_core),
            }
            token_path = base / "authorization-token.json"
            write_json(token_path, token)
            with self.assertRaisesRegex(
                compare_runs.ArtifactError, "exact raw N=100 parallel driver"
            ):
                compare_runs.snapshot_authorized_rerun_provenance(
                    MODULE_PATH.with_name("parallel_driver.py"),
                    token_path,
                    raw_provenance,
                    dataset,
                    manifest,
                    200,
                    "gpt-5",
                    "guarded",
                    None,
                    True,
                    True,
                    7200,
                    base,
                )

            unknown = json.loads(json.dumps(REAL_N90_PROVENANCE))
            unknown["driver"]["sha256"] = "f" * 64
            unknown["fingerprint"] = compare_runs.canonical_sha256(
                compare_runs._drop_keys(unknown, {"fingerprint"})
            )
            with self.assertRaisesRegex(
                compare_runs.ArtifactError,
                "missing selector policy is not attested",
            ):
                compare_runs.remediation_policy_semantics(unknown)

            partial = json.loads(json.dumps(REAL_N90_PROVENANCE))
            partial["policy"]["selector_policy"] = "replay-v1"
            with self.assertRaisesRegex(
                compare_runs.ArtifactError,
                "partial selector-policy surface",
            ):
                compare_runs.remediation_policy_semantics(partial)

    def test_remediation_rerun_rejects_extra_attempt_cherry_pick_and_policy_drift(self):
        variants = (
            "valid",
            "extra_attempt",
            "extra_root_terminal",
            "nested_terminal",
            "partial_residue",
            "stale_residue",
            "missing_required",
            "resumed_attempt",
            "cherry_pick",
            "policy_drift",
        )
        for variant in variants:
            with (
                self.subTest(variant=variant),
                tempfile.TemporaryDirectory() as directory,
            ):
                base = Path(directory).resolve()
                raw = base / "raw-checkpoint"
                attempt = base / "declaration.attempt"
                rerun = attempt / "rerun"
                target = "task-remediate"
                write_remediation_rerun(raw, rerun, target)
                authorization = write_remediation_authorization(raw, attempt, target)
                if variant == "extra_attempt":
                    extra = rerun / "runs" / "second-attempt"
                    extra.mkdir()
                    write_json(extra / "run_record.json", {"status": "success"})
                elif variant == "extra_root_terminal":
                    write_json(rerun / "run_record.json", {"status": "success"})
                elif variant == "nested_terminal":
                    write_json(
                        rerun / "runs" / target / "nested" / "run_record.json",
                        {"status": "success"},
                    )
                elif variant == "partial_residue":
                    write_jsonl(
                        rerun / "runs" / target / "prediction.partial.jsonl",
                        [prediction(target)],
                    )
                elif variant == "stale_residue":
                    write_json(
                        rerun / "runs" / target / "query-plan.stale.json",
                        {target: ["old"]},
                    )
                elif variant == "missing_required":
                    (rerun / "runs" / target / "runner.log").unlink()
                elif variant == "resumed_attempt":
                    summary = json.loads((rerun / "driver_summary.json").read_text())
                    summary["per_instance"][target]["skipped"] = True
                    summary["per_instance"][target]["seconds"] = 0.0
                    write_json(rerun / "driver_summary.json", summary)
                elif variant == "cherry_pick":
                    row = prediction(target)
                    row["traj_data"]["pred_steps"] = [{"files": ["picked.py"]}]
                    write_jsonl(rerun / "predictions.jsonl", [row])
                elif variant == "policy_drift":
                    provenance_value = json.loads(
                        (rerun / "run_provenance.json").read_text()
                    )
                    provenance_value["policy"]["line_budget"] = 199
                    write_json(rerun / "run_provenance.json", provenance_value)

                if variant == "valid":
                    validated = compare_runs.validate_remediation_rerun(
                        raw, rerun, target, authorization
                    )
                    self.assertEqual(validated["target_id"], target)
                    self.assertTrue(validated["parity"]["same_model_and_policy"])
                else:
                    with self.assertRaises(compare_runs.ArtifactError):
                        compare_runs.validate_remediation_rerun(
                            raw, rerun, target, authorization
                        )

    def test_remediation_authorization_binds_exact_attempt_and_is_append_only(self):
        variants = (
            "valid",
            "raw_checkpoint_drift",
            "token_nonce_drift",
            "manifest_drift",
            "rerun_id_drift",
            "fingerprint_drift",
            "third_ledger_entry",
            "wrong_target",
        )
        for variant in variants:
            with (
                self.subTest(variant=variant),
                tempfile.TemporaryDirectory() as directory,
            ):
                base = Path(directory).resolve()
                raw = base / "raw-checkpoint"
                attempt = base / "declaration.attempt"
                rerun = attempt / "rerun"
                target = "task-remediate"
                write_remediation_rerun(raw, rerun, target)
                authorization = write_remediation_authorization(raw, attempt, target)
                requested_target = target
                if variant == "raw_checkpoint_drift":
                    write_json(raw / "checkpoint.json", {"schema_version": 2})
                elif variant == "token_nonce_drift":
                    token = json.loads(
                        (attempt / "authorization-token.json").read_text()
                    )
                    token["nonce"] = "x" * 32
                    write_json(attempt / "authorization-token.json", token)
                elif variant == "manifest_drift":
                    with (attempt / "rerun-manifest.json").open(
                        "a", encoding="utf-8"
                    ) as out:
                        out.write(" \n")
                elif variant == "rerun_id_drift":
                    meta = json.loads((rerun / "run_meta.json").read_text())
                    meta["run_id"] = "rerun-2"
                    write_json(rerun / "run_meta.json", meta)
                elif variant == "fingerprint_drift":
                    provenance = json.loads((rerun / "run_provenance.json").read_text())
                    provenance["fingerprint"] = "fp-second"
                    write_json(rerun / "run_provenance.json", provenance)
                elif variant == "third_ledger_entry":
                    rows = [
                        json.loads(line)
                        for line in (attempt / "attempt-ledger.jsonl")
                        .read_text()
                        .splitlines()
                    ]
                    write_jsonl(
                        attempt / "attempt-ledger.jsonl", rows + [rows[0], rows[0]]
                    )
                elif variant == "wrong_target":
                    requested_target = "other-task"

                if variant == "valid":
                    loaded = compare_runs.load_remediation_execution_authorization(
                        authorization, raw, requested_target
                    )
                    self.assertEqual(loaded["token"]["nonce"], "n" * 32)
                    validated = compare_runs.validate_remediation_rerun(
                        raw, rerun, target, authorization
                    )
                    recorded = validated["parity"]["recorded_resource_identity"]
                    self.assertIn("not asserted", recorded["scope"])
                    self.assertTrue(
                        recorded["all_other_recorded_resource_fields_identical"]
                    )
                else:
                    with self.assertRaises(compare_runs.ArtifactError):
                        if variant in {"rerun_id_drift", "fingerprint_drift"}:
                            compare_runs.validate_remediation_rerun(
                                raw, rerun, target, authorization
                            )
                        else:
                            compare_runs.load_remediation_execution_authorization(
                                authorization, raw, requested_target
                            )

    def test_remediation_bundle_is_one_shot_hash_bound_and_changes_only_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            control_root = base / "control"
            checkpoint_root = base / "checkpoints"
            candidate_root = base / "candidate"
            for path in (control_root, checkpoint_root, candidate_root):
                path.mkdir()
            (control_root / "watchdog.log").write_text("", encoding="utf-8")
            ids = [f"task-{index:03d}" for index in range(100)]
            target = ids[45]
            target_line = (
                f"outcome=timeout slug={target} pid=1 elapsed_s=4123168608 "
                "limit_s=5400\n"
            )
            latest = checkpoint_root / "checkpoint-0100"
            latest.mkdir()
            (latest / "manifest.json").write_text(
                json.dumps(ids, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            (latest / "watchdog.log").write_text(target_line, encoding="utf-8")
            write_json(latest / "checkpoint.json", {"schema_version": 1})
            write_json(latest / "batch-manifest.json", ids[-10:])
            origin = checkpoint_root / "checkpoint-0050"
            origin.mkdir()
            write_json(origin / "manifest.json", ids[:50])
            write_json(origin / "batch-manifest.json", ids[40:50])
            write_json(origin / "checkpoint.json", {"schema_version": 1, "count": 50})
            (origin / "watchdog.log").write_text(target_line, encoding="utf-8")

            run_record_relative = f"runs/{target}/run_record.json"
            prediction_relative = f"runs/{target}/prediction.jsonl"
            audit_relative = f"runs/{target}/prediction-audit/{target}.json"
            failure_relative = f"runs/{target}/failure.json"
            raw_failure = {
                "instance_id": target,
                "returncode": -9,
                "run_fingerprint": "fp-raw",
                "seconds": 0.4,
            }
            raw_failure_prediction = prediction(target, failure=True)
            raw_failure_prediction["harness_failure"]["run_fingerprint"] = "fp-raw"
            for source_root in (latest, origin):
                source = source_root / "source-artifacts"
                write_json(
                    source / run_record_relative,
                    {
                        "instance_id": target,
                        "status": "failure",
                        "failure_kind": "nonzero_exit",
                        "run_fingerprint": "fp-raw",
                    },
                )
                write_jsonl(
                    source / prediction_relative,
                    [raw_failure_prediction],
                )
                write_json(
                    source / audit_relative,
                    {"harness_failure": raw_failure},
                )
                write_json(
                    source / failure_relative,
                    raw_failure,
                )
            origin_source = origin / "source-artifacts"
            run_record_sha = hashlib.sha256(
                (origin_source / run_record_relative).read_bytes()
            ).hexdigest()
            prediction_sha = hashlib.sha256(
                (origin_source / prediction_relative).read_bytes()
            ).hexdigest()
            audit_sha = hashlib.sha256(
                (origin_source / audit_relative).read_bytes()
            ).hexdigest()
            failure_sha = hashlib.sha256(
                (origin_source / failure_relative).read_bytes()
            ).hexdigest()
            raw_record = {
                "status": "failure",
                "seconds": 0.4,
                "failure_kind": "nonzero_exit",
                "run_record_mtime_ns": 1,
                "run_record_sha256": run_record_sha,
                "prediction_sha256": prediction_sha,
                "audit_sha256": audit_sha,
                "query_plan_sha256": None,
                "failure_sha256": failure_sha,
                "validation_mode": "legacy_failure_semantic_attestation_v1",
                "run_record_relative_path": run_record_relative,
                "prediction_relative_path": prediction_relative,
                "audit_relative_path": audit_relative,
                "failure_relative_path": failure_relative,
                "legacy_failure_attestation": {
                    "exact_zero_prediction_stub_verified": True,
                    "audit_exactly_matches_failure": True,
                    "query_plan_absent": True,
                    "frozen_audit_sha256": audit_sha,
                    "frozen_failure_sha256": failure_sha,
                    "failure_returncode": -9,
                },
            }
            write_json(
                latest / "completion-records.json",
                {"per_instance": {target: raw_record}},
            )
            write_json(
                origin / "completion-records.json",
                {"per_instance": {target: raw_record}},
            )

            control_predictions = {
                instance_id: prediction(instance_id) for instance_id in ids
            }
            raw_predictions = json.loads(json.dumps(control_predictions))
            raw_predictions[target] = prediction(target, failure=True)
            control_results = {
                instance_id: result(instance_id, 0.2) for instance_id in ids
            }
            raw_results = {instance_id: result(instance_id, 0.4) for instance_id in ids}
            raw_results[target] = {
                "instance_id": target,
                "error": "no_context_extracted",
            }
            control_records = {
                instance_id: {"status": "success", "seconds": 1.0}
                for instance_id in ids
            }
            candidate_records = json.loads(json.dumps(control_records))
            candidate_records[target] = raw_record
            languages = {instance_id: "python" for instance_id in ids}
            control_run = compare_runs.synthetic_run(
                ids,
                control_predictions,
                control_results,
                languages,
                control_records,
                control_root / "watchdog.log",
            )
            raw_run = compare_runs.synthetic_run(
                ids,
                raw_predictions,
                raw_results,
                languages,
                candidate_records,
                latest / "watchdog.log",
            )

            evaluator = base / "evaluate.py"
            evaluator.write_text("# evaluator fixture\n", encoding="utf-8")
            contextbench_checkout = base / "contextbench-checkout"
            contextbench_checkout.mkdir()
            runtime_manifest = base / "runtime.json"
            runtime = {
                "schema_version": 1,
                "python": {"executable": "/fixture/python"},
                "contextbench_checkout": {"path": str(contextbench_checkout)},
                "gold_parquet": {"path": "/fixture/gold.parquet"},
                "evaluator": {
                    "path": str(evaluator),
                    "sha256": hashlib.sha256(evaluator.read_bytes()).hexdigest(),
                },
                "environment": {
                    "PYTHONPATH": str(contextbench_checkout),
                    "SYMBOL_DETAIL_MAX": "0",
                },
            }
            write_json(runtime_manifest, runtime)
            declaration_core = {
                "schema_version": 1,
                "mode": "predeclared-false-kill-remediation",
                "target_ids": [target],
                "false_kill_attestation": {
                    "mode": "impossible_watchdog_elapsed_false_kill_v1",
                    "target_id": target,
                    "originating_checkpoint_count": 50,
                    "originating_checkpoint_sha256": hashlib.sha256(
                        (origin / "checkpoint.json").read_bytes()
                    ).hexdigest(),
                    "run_record_sha256": raw_record["run_record_sha256"],
                    "prediction_sha256": raw_record["prediction_sha256"],
                    "audit_sha256": raw_record["audit_sha256"],
                    "failure_sha256": raw_record["failure_sha256"],
                    "watchdog_line_sha256": hashlib.sha256(
                        target_line.encode("utf-8")
                    ).hexdigest(),
                    "watchdog_elapsed_seconds": 4123168608.0,
                    "watchdog_limit_seconds": 5400.0,
                    "recorded_runner_seconds": 0.4,
                    "raw_failure_record": raw_record,
                },
            }
            declaration = {
                **declaration_core,
                "declaration_sha256": compare_runs.canonical_sha256(declaration_core),
            }
            declaration_path = base / "declaration.json"
            write_json(declaration_path, declaration)
            attempt_root = compare_runs.canonical_remediation_attempt_paths(
                checkpoint_root, declaration
            )["attempt_root"]
            rerun_root = attempt_root / "rerun"
            write_remediation_rerun(latest, rerun_root, target)
            authorization_path = write_remediation_authorization(
                latest,
                attempt_root,
                target,
                declaration_path=declaration_path,
                origin_checkpoint=origin,
            )
            chain = []
            for count in range(10, 101, 10):
                checkpoint = checkpoint_root / f"checkpoint-{count:04d}"
                checkpoint.mkdir(exist_ok=True)
                if not (checkpoint / "manifest.json").is_file():
                    write_json(checkpoint / "manifest.json", ids[:count])
                if not (checkpoint / "batch-manifest.json").is_file():
                    write_json(
                        checkpoint / "batch-manifest.json", ids[count - 10 : count]
                    )
                if not (checkpoint / "checkpoint.json").is_file():
                    write_json(
                        checkpoint / "checkpoint.json",
                        {"schema_version": 1, "count": count},
                    )
                if not (checkpoint / "watchdog.log").is_file():
                    (checkpoint / "watchdog.log").write_text(
                        target_line if count >= 50 else "", encoding="utf-8"
                    )
                batch_ids = ids[count - 10 : count]
                write_jsonl(
                    checkpoint / "control-batch-predictions.jsonl",
                    [control_predictions[instance_id] for instance_id in batch_ids],
                )
                write_jsonl(
                    checkpoint / "candidate-batch-predictions.jsonl",
                    [raw_predictions[instance_id] for instance_id in batch_ids],
                )
                write_jsonl(
                    checkpoint / "evaluation/control-results.jsonl",
                    [control_results[instance_id] for instance_id in batch_ids],
                )
                write_jsonl(
                    checkpoint / "evaluation/candidate-results.jsonl",
                    [raw_results[instance_id] for instance_id in batch_ids],
                )
                write_jsonl(
                    checkpoint / "control-triage.jsonl",
                    [
                        {
                            "instance_id": instance_id,
                            "language": languages[instance_id],
                        }
                        for instance_id in ids[:count]
                    ],
                )
                chain.append(
                    {
                        "dir": checkpoint,
                        "ids": ids[:count],
                        "batch_ids": batch_ids,
                    }
                )
            evaluator_sha = hashlib.sha256(evaluator.read_bytes()).hexdigest()
            runtime_sha = hashlib.sha256(runtime_manifest.read_bytes()).hexdigest()
            state = {
                "ids": ids,
                "chain": chain,
                "latest": {"dir": latest, "ids": ids, "batch_ids": ids[-10:]},
                "control": control_run,
                "raw_candidate": raw_run,
                "raw_report": {
                    "validation": {"n_100_full_reconciliation": {"complete": True}},
                    "gate": {"passed": False, "release_eligible": False},
                },
                "languages": languages,
                "evaluator_sha256": evaluator_sha,
                "runtime_manifest_sha256": runtime_sha,
                "checkpoint_bindings": [
                    {
                        "count": count,
                        "checkpoint_sha256": hashlib.sha256(
                            (
                                checkpoint_root
                                / f"checkpoint-{count:04d}"
                                / "checkpoint.json"
                            ).read_bytes()
                        ).hexdigest(),
                        "runtime_manifest_sha256": runtime_sha,
                        "evaluator_sha256": evaluator_sha,
                        "batch_manifest_sha256": hashlib.sha256(
                            (
                                checkpoint_root
                                / f"checkpoint-{count:04d}"
                                / "batch-manifest.json"
                            ).read_bytes()
                        ).hexdigest(),
                        "run_meta_sha256": hashlib.sha256(
                            (latest / "run_meta.json").read_bytes()
                        ).hexdigest(),
                        "run_provenance_sha256": hashlib.sha256(
                            (latest / "run_provenance.json").read_bytes()
                        ).hexdigest(),
                        "control_batch_predictions_sha256": hashlib.sha256(
                            (
                                checkpoint_root
                                / f"checkpoint-{count:04d}"
                                / "control-batch-predictions.jsonl"
                            ).read_bytes()
                        ).hexdigest(),
                        "candidate_batch_predictions_sha256": hashlib.sha256(
                            (
                                checkpoint_root
                                / f"checkpoint-{count:04d}"
                                / "candidate-batch-predictions.jsonl"
                            ).read_bytes()
                        ).hexdigest(),
                        "control_results_sha256": hashlib.sha256(
                            (
                                checkpoint_root
                                / f"checkpoint-{count:04d}"
                                / "evaluation/control-results.jsonl"
                            ).read_bytes()
                        ).hexdigest(),
                        "candidate_results_sha256": hashlib.sha256(
                            (
                                checkpoint_root
                                / f"checkpoint-{count:04d}"
                                / "evaluation/candidate-results.jsonl"
                            ).read_bytes()
                        ).hexdigest(),
                        "watchdog_sha256": hashlib.sha256(
                            (
                                checkpoint_root
                                / f"checkpoint-{count:04d}"
                                / "watchdog.log"
                            ).read_bytes()
                        ).hexdigest(),
                        "triage_sha256": hashlib.sha256(
                            (
                                checkpoint_root
                                / f"checkpoint-{count:04d}"
                                / "control-triage.jsonl"
                            ).read_bytes()
                        ).hexdigest(),
                    }
                    for count in range(10, 101, 10)
                ],
            }
            self.assertTrue((latest / "control-triage.jsonl").is_file())
            self.assertFalse((latest / "triage").exists())

            evaluator_calls = []

            def fake_evaluator(command, **kwargs):
                evaluator_calls.append(command)
                write_jsonl(kwargs["result_path"], [result(target, 0.6)])
                kwargs["stdout_path"].write_text("stdout\n", encoding="utf-8")
                kwargs["stderr_path"].write_text("", encoding="utf-8")
                return {
                    "command": command,
                    "cwd": str(kwargs["cwd"]),
                    "environment": {
                        key: kwargs["environment"][key]
                        for key in (
                            "PYTHONPATH",
                            "SYMBOL_DETAIL_MAX",
                            "PYTHONDONTWRITEBYTECODE",
                            "PYTHONNOUSERSITE",
                        )
                    },
                    "returncode": 0,
                    "prediction_sha256": hashlib.sha256(
                        kwargs["prediction_path"].read_bytes()
                    ).hexdigest(),
                    "result_sha256": hashlib.sha256(
                        kwargs["result_path"].read_bytes()
                    ).hexdigest(),
                    "stdout_sha256": hashlib.sha256(
                        kwargs["stdout_path"].read_bytes()
                    ).hexdigest(),
                    "stderr_sha256": hashlib.sha256(
                        kwargs["stderr_path"].read_bytes()
                    ).hexdigest(),
                }

            bundle = attempt_root / "bundle"
            with (
                mock.patch.object(
                    compare_runs,
                    "verify_false_kill_declaration",
                    return_value=target,
                ),
                mock.patch.object(
                    compare_runs,
                    "load_n_100_remediation_state",
                    return_value=state,
                ),
                mock.patch.object(
                    compare_runs,
                    "validate_runtime_manifest",
                    return_value=runtime,
                ),
                mock.patch.object(
                    compare_runs,
                    "run_evaluator_command",
                    side_effect=fake_evaluator,
                ),
            ):
                forbidden_json = rerun_root / "forbidden-output.json"
                exit_code = compare_runs.main(
                    [
                        "remediate-false-kill",
                        "--control",
                        str(control_root),
                        "--checkpoint-root",
                        str(checkpoint_root),
                        "--candidate-run",
                        str(candidate_root),
                        "--declaration",
                        str(declaration_path),
                        "--authorization",
                        str(authorization_path),
                        "--rerun",
                        str(rerun_root),
                        "--evaluator",
                        str(evaluator),
                        "--runtime-manifest",
                        str(runtime_manifest),
                        "--cache-dir",
                        str(base / "cache"),
                        "--bundle-root",
                        str(bundle),
                        "--json-out",
                        str(forbidden_json),
                    ]
                )
                self.assertEqual(exit_code, 2)
                self.assertFalse(forbidden_json.exists())
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError, "canonical exclusive destination"
                ):
                    compare_runs.create_remediation_bundle(
                        control_root,
                        checkpoint_root,
                        candidate_root,
                        declaration_path,
                        authorization_path,
                        rerun_root,
                        evaluator,
                        runtime_manifest,
                        base / "cache",
                        base / "noncanonical-bundle",
                    )
                with self.assertRaisesRegex(compare_runs.ArtifactError, "overlaps"):
                    compare_runs.create_remediation_bundle(
                        control_root,
                        checkpoint_root,
                        candidate_root,
                        declaration_path,
                        authorization_path,
                        rerun_root,
                        evaluator,
                        runtime_manifest,
                        rerun_root / "cache",
                        bundle,
                    )
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError, "pinned ContextBench checkout"
                ):
                    compare_runs.create_remediation_bundle(
                        control_root,
                        checkpoint_root,
                        candidate_root,
                        declaration_path,
                        authorization_path,
                        rerun_root,
                        evaluator,
                        runtime_manifest,
                        contextbench_checkout / "cache",
                        bundle,
                    )
                frozen_triage_sha256 = state["checkpoint_bindings"][-1]["triage_sha256"]
                state["checkpoint_bindings"][-1]["triage_sha256"] = "0" * 64
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError,
                    "control triage differs from its frozen binding",
                ):
                    compare_runs.create_remediation_bundle(
                        control_root,
                        checkpoint_root,
                        candidate_root,
                        declaration_path,
                        authorization_path,
                        rerun_root,
                        evaluator,
                        runtime_manifest,
                        base / "cache",
                        bundle,
                    )
                self.assertEqual(evaluator_calls, [])
                state["checkpoint_bindings"][-1]["triage_sha256"] = frozen_triage_sha256
                output = compare_runs.create_remediation_bundle(
                    control_root,
                    checkpoint_root,
                    candidate_root,
                    declaration_path,
                    authorization_path,
                    rerun_root,
                    evaluator,
                    runtime_manifest,
                    base / "cache",
                    bundle,
                )
                self.assertEqual(len(evaluator_calls), 1)
                self.assertTrue(output["comparison"]["gate"]["release_eligible"])
                self.assertFalse(output["raw_history_rewritten"])
                verified = compare_runs.verify_remediation_bundle(bundle)
                self.assertEqual(verified["target_id"], target)
                self.assertTrue(verified["release_eligible"])
                self.assertTrue(all(verified["independently_recomputed"].values()))
                self.assertTrue(
                    verified["gate"]["repaired_originating_batch_gate"]["passed"]
                )
                normal_seal = json.loads((bundle / "remediation.json").read_text())
                self.assertEqual(
                    normal_seal["evaluator"]["total_official_evaluator_invocations"],
                    1,
                )
                self.assertEqual(
                    normal_seal["evaluator"]["prior_unsealed_operator_attested"],
                    0,
                )
                self.assertEqual(normal_seal["evaluator"]["sealed_receipts"], 1)
                self.assertEqual(normal_seal["evaluator"]["aws_execution_attempts"], 1)
                normal_comparison = json.loads((bundle / "comparison.json").read_text())
                self.assertEqual(
                    normal_comparison["validation"]["official_evaluator_invocations"],
                    1,
                )
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError, "second remediation attempt"
                ):
                    compare_runs.create_remediation_bundle(
                        control_root,
                        checkpoint_root,
                        candidate_root,
                        declaration_path,
                        authorization_path,
                        rerun_root,
                        evaluator,
                        runtime_manifest,
                        base / "cache",
                        bundle,
                    )

                recovery_declaration_core = {
                    **declaration_core,
                    "test_fixture_attempt": "packaging-retry",
                }
                recovery_declaration = {
                    **recovery_declaration_core,
                    "declaration_sha256": compare_runs.canonical_sha256(
                        recovery_declaration_core
                    ),
                }
                recovery_declaration_path = base / "recovery-declaration.json"
                write_json(recovery_declaration_path, recovery_declaration)
                recovery_attempt = compare_runs.canonical_remediation_attempt_paths(
                    checkpoint_root, recovery_declaration
                )["attempt_root"]
                recovery_rerun = recovery_attempt / "rerun"
                write_remediation_rerun(latest, recovery_rerun, target)
                recovery_authorization = write_remediation_authorization(
                    latest,
                    recovery_attempt,
                    target,
                    declaration_path=recovery_declaration_path,
                    origin_checkpoint=origin,
                )
                recovery_validated = compare_runs.validate_remediation_rerun(
                    latest,
                    recovery_rerun,
                    target,
                    recovery_authorization,
                )
                compare_runs.bind_rerun_observation_to_ledger(recovery_validated)
                forbidden_recovery_json = recovery_attempt / "predeclare-output.json"
                self.assertEqual(
                    compare_runs.main(
                        [
                            "predeclare-packaging-retry",
                            "--control",
                            str(control_root),
                            "--checkpoint-root",
                            str(checkpoint_root),
                            "--candidate-run",
                            str(candidate_root),
                            "--declaration",
                            str(recovery_declaration_path),
                            "--authorization",
                            str(recovery_authorization),
                            "--rerun",
                            str(recovery_rerun),
                            "--evaluator",
                            str(evaluator),
                            "--runtime-manifest",
                            str(runtime_manifest),
                            "--failed-packager-sha256",
                            compare_runs.PACKAGING_RETRY_FAILED_PACKAGER_SHA256,
                            "--json-out",
                            str(forbidden_recovery_json),
                        ]
                    ),
                    2,
                )
                self.assertFalse(forbidden_recovery_json.exists())
                recovery_predeclaration_path = base / "packaging-retry-predeclare.json"
                recovery_predeclare_exit = compare_runs.main(
                    [
                        "predeclare-packaging-retry",
                        "--control",
                        str(control_root),
                        "--checkpoint-root",
                        str(checkpoint_root),
                        "--candidate-run",
                        str(candidate_root),
                        "--declaration",
                        str(recovery_declaration_path),
                        "--authorization",
                        str(recovery_authorization),
                        "--rerun",
                        str(recovery_rerun),
                        "--evaluator",
                        str(evaluator),
                        "--runtime-manifest",
                        str(runtime_manifest),
                        "--failed-packager-sha256",
                        compare_runs.PACKAGING_RETRY_FAILED_PACKAGER_SHA256,
                        "--json-out",
                        str(recovery_predeclaration_path),
                    ]
                )
                self.assertEqual(recovery_predeclare_exit, 0)
                recovery_predeclaration = json.loads(
                    recovery_predeclaration_path.read_text()
                )
                recovery_paths = compare_runs.packaging_retry_paths(recovery_attempt)
                self.assertEqual(
                    Path(recovery_predeclaration["attestation"]),
                    recovery_paths["attestation"],
                )
                recovery_token = json.loads(
                    (recovery_attempt / "authorization-token.json").read_text()
                )
                self.assertNotIn(
                    "packaging_retry_attestation",
                    recovery_token["canonical_paths"],
                )
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError,
                    "already predeclared or consumed",
                ):
                    compare_runs.predeclare_packaging_retry(
                        control_root,
                        checkpoint_root,
                        candidate_root,
                        recovery_declaration_path,
                        recovery_authorization,
                        recovery_rerun,
                        evaluator,
                        runtime_manifest,
                        compare_runs.PACKAGING_RETRY_FAILED_PACKAGER_SHA256,
                    )

                attestation_bytes = recovery_paths["attestation"].read_bytes()
                attestation = json.loads(attestation_bytes)
                self.assertEqual(
                    attestation["operator_attestation"]["correct_frozen_path"],
                    "control-triage.jsonl",
                )
                self.assertEqual(
                    attestation["operator_attestation"]["correct_frozen_sha256"],
                    state["checkpoint_bindings"][-1]["triage_sha256"],
                )
                attestation["operator_attestation"][
                    "prior_receipt_or_result_survived"
                ] = True
                write_json(recovery_paths["attestation"], attestation)
                evaluator_calls.clear()
                recovery_bundle = recovery_attempt / "bundle"
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError,
                    "attestation hash",
                ):
                    compare_runs.create_remediation_bundle(
                        control_root,
                        checkpoint_root,
                        candidate_root,
                        recovery_declaration_path,
                        recovery_authorization,
                        recovery_rerun,
                        evaluator,
                        runtime_manifest,
                        base / "recovery-cache",
                        recovery_bundle,
                    )
                self.assertEqual(evaluator_calls, [])
                recovery_paths["attestation"].write_bytes(attestation_bytes)

                with (
                    mock.patch.object(
                        compare_runs,
                        "fsync_file",
                        wraps=compare_runs.fsync_file,
                    ) as durable_files,
                    mock.patch.object(
                        compare_runs,
                        "fsync_directory",
                        wraps=compare_runs.fsync_directory,
                    ) as durable_directories,
                    mock.patch.object(
                        compare_runs,
                        "verify_remediation_bundle",
                        side_effect=compare_runs.ArtifactError(
                            "forced post-evaluation assembly failure"
                        ),
                    ),
                ):
                    with self.assertRaisesRegex(
                        compare_runs.ArtifactError,
                        "forced post-evaluation assembly failure",
                    ):
                        compare_runs.create_remediation_bundle(
                            control_root,
                            checkpoint_root,
                            candidate_root,
                            recovery_declaration_path,
                            recovery_authorization,
                            recovery_rerun,
                            evaluator,
                            runtime_manifest,
                            base / "recovery-cache",
                            recovery_bundle,
                        )
                self.assertEqual(len(evaluator_calls), 1)
                self.assertFalse(recovery_bundle.exists())
                self.assertTrue(recovery_paths["consumption"].is_file())
                self.assertTrue(recovery_paths["evaluation"].is_dir())
                self.assertEqual(
                    {call.args[0].name for call in durable_files.call_args_list},
                    compare_runs.PACKAGING_RETRY_EVALUATION_FILES,
                )
                self.assertIn(
                    recovery_attempt,
                    [call.args[0] for call in durable_directories.call_args_list],
                )
                consumption_record = json.loads(
                    recovery_paths["consumption"].read_text()
                )
                self.assertEqual(
                    consumption_record["invocation_accounting_after_consumption"][
                        "observed_prior_official_evaluator_invocations"
                    ],
                    1,
                )
                self.assertEqual(
                    consumption_record["invocation_accounting_after_consumption"][
                        "retry_invocation_number_reserved"
                    ],
                    2,
                )

                with self.assertRaises(FileExistsError):
                    compare_runs.write_exclusive(
                        recovery_paths["consumption"], b"second consumption\n"
                    )
                consumption_bytes = recovery_paths["consumption"].read_bytes()
                consumption = json.loads(consumption_bytes)
                consumption["retry_evaluator_invocation_number"] = 3
                write_json(recovery_paths["consumption"], consumption)
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError,
                    "consumption hash",
                ):
                    compare_runs.create_remediation_bundle(
                        control_root,
                        checkpoint_root,
                        candidate_root,
                        recovery_declaration_path,
                        recovery_authorization,
                        recovery_rerun,
                        evaluator,
                        runtime_manifest,
                        base / "recovery-cache",
                        recovery_bundle,
                    )
                self.assertEqual(len(evaluator_calls), 1)
                recovery_paths["consumption"].write_bytes(consumption_bytes)

                persisted_result = recovery_paths["evaluation"] / "result.jsonl"
                persisted_result_bytes = persisted_result.read_bytes()
                persisted_result.write_text("tampered\n", encoding="utf-8")
                with self.assertRaises(compare_runs.ArtifactError):
                    compare_runs.create_remediation_bundle(
                        control_root,
                        checkpoint_root,
                        candidate_root,
                        recovery_declaration_path,
                        recovery_authorization,
                        recovery_rerun,
                        evaluator,
                        runtime_manifest,
                        base / "recovery-cache",
                        recovery_bundle,
                    )
                self.assertEqual(len(evaluator_calls), 1)
                persisted_result.write_bytes(persisted_result_bytes)

                persisted_evaluation_backup = base / "retry-evaluation-backup"
                os.rename(recovery_paths["evaluation"], persisted_evaluation_backup)
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError,
                    "consumed without a complete persisted evaluation",
                ):
                    compare_runs.create_remediation_bundle(
                        control_root,
                        checkpoint_root,
                        candidate_root,
                        recovery_declaration_path,
                        recovery_authorization,
                        recovery_rerun,
                        evaluator,
                        runtime_manifest,
                        base / "recovery-cache",
                        recovery_bundle,
                    )
                self.assertEqual(len(evaluator_calls), 1)
                os.rename(persisted_evaluation_backup, recovery_paths["evaluation"])

                recovery_output = compare_runs.create_remediation_bundle(
                    control_root,
                    checkpoint_root,
                    candidate_root,
                    recovery_declaration_path,
                    recovery_authorization,
                    recovery_rerun,
                    evaluator,
                    runtime_manifest,
                    base / "recovery-cache",
                    recovery_bundle,
                )
                self.assertEqual(len(evaluator_calls), 1)
                self.assertTrue(
                    recovery_output["comparison"]["gate"]["release_eligible"]
                )
                recovery_verified = compare_runs.verify_remediation_bundle(
                    recovery_bundle
                )
                self.assertTrue(recovery_verified["release_eligible"])
                recovery_seal = json.loads(
                    (recovery_bundle / "remediation.json").read_text()
                )
                self.assertTrue(recovery_seal["packaging_retry_recovery"])
                self.assertEqual(
                    recovery_seal["evaluator"]["total_official_evaluator_invocations"],
                    2,
                )
                self.assertEqual(
                    recovery_seal["evaluator"]["prior_unsealed_operator_attested"],
                    1,
                )
                self.assertEqual(recovery_seal["evaluator"]["sealed_receipts"], 1)
                self.assertEqual(
                    recovery_seal["evaluator"]["aws_execution_attempts"], 1
                )
                recovery_comparison = json.loads(
                    (recovery_bundle / "comparison.json").read_text()
                )
                self.assertEqual(
                    recovery_comparison["validation"]["official_evaluator_invocations"],
                    2,
                )
                tampered_recovery_count = base / "tampered-recovery-count-bundle"
                shutil.copytree(recovery_bundle, tampered_recovery_count)
                recovery_comparison["validation"]["official_evaluator_invocations"] = 1
                write_json(
                    tampered_recovery_count / "comparison.json",
                    recovery_comparison,
                )
                reseal_remediation_bundle(tampered_recovery_count, "comparison.json")
                with self.assertRaisesRegex(
                    compare_runs.ArtifactError,
                    "standalone sealed-input recomputation",
                ):
                    compare_runs.verify_remediation_bundle(tampered_recovery_count)

            tampered = base / "tampered-resealed-bundle"
            shutil.copytree(bundle, tampered)
            tampered_comparison = json.loads((tampered / "comparison.json").read_text())
            tampered_comparison["gate"]["passed"] = False
            tampered_comparison["gate"]["release_eligible"] = False
            write_json(tampered / "comparison.json", tampered_comparison)
            reseal_remediation_bundle(tampered, "comparison.json")
            with self.assertRaisesRegex(
                compare_runs.ArtifactError, "standalone sealed-input recomputation"
            ):
                compare_runs.verify_remediation_bundle(tampered)

            tampered_raw_gate = base / "tampered-raw-gate-bundle"
            shutil.copytree(bundle, tampered_raw_gate)
            raw_gate_comparison = json.loads(
                (tampered_raw_gate / "comparison.json").read_text()
            )
            raw_gate_comparison["raw_gate"]["passed"] = not raw_gate_comparison[
                "raw_gate"
            ]["passed"]
            write_json(tampered_raw_gate / "comparison.json", raw_gate_comparison)
            reseal_remediation_bundle(tampered_raw_gate, "comparison.json")
            with self.assertRaisesRegex(
                compare_runs.ArtifactError, "standalone sealed-input recomputation"
            ):
                compare_runs.verify_remediation_bundle(tampered_raw_gate)

            tampered_parity = base / "tampered-parity-bundle"
            shutil.copytree(bundle, tampered_parity)
            parity_seal = json.loads((tampered_parity / "remediation.json").read_text())
            parity_seal["parity"]["recorded_resource_identity"]["scope"] = (
                "exact physical-host equivalence asserted"
            )
            write_json(tampered_parity / "remediation.json", parity_seal)
            with self.assertRaisesRegex(
                compare_runs.ArtifactError, "parity is not independently reproducible"
            ):
                compare_runs.verify_remediation_bundle(tampered_parity)

            tampered_raw_bytes = base / "tampered-raw-provenance-bytes-bundle"
            shutil.copytree(bundle, tampered_raw_bytes)
            token_path = tampered_raw_bytes / "authorization-token.json"
            token = json.loads(token_path.read_text())
            token["raw_n_100"]["run_provenance_bytes"] += 1
            token["token_sha256"] = compare_runs.canonical_sha256(
                compare_runs._drop_keys(token, {"token_sha256"})
            )
            write_json(token_path, token)
            authorization_path = tampered_raw_bytes / "authorization.json"
            authorization = json.loads(authorization_path.read_text())
            authorization["token_sha256"] = token["token_sha256"]
            authorization["token_file_sha256"] = hashlib.sha256(
                token_path.read_bytes()
            ).hexdigest()
            authorization["authorization_sha256"] = compare_runs.canonical_sha256(
                compare_runs._drop_keys(authorization, {"authorization_sha256"})
            )
            write_json(authorization_path, authorization)
            ledger_path = tampered_raw_bytes / "attempt-ledger.jsonl"
            ledger = [json.loads(line) for line in ledger_path.read_text().splitlines()]
            for entry in ledger:
                entry["authorization_sha256"] = authorization["authorization_sha256"]
                if entry["sequence"] == 1:
                    entry["authorization_file_sha256"] = hashlib.sha256(
                        authorization_path.read_bytes()
                    ).hexdigest()
                entry["entry_sha256"] = compare_runs.canonical_sha256(
                    compare_runs._drop_keys(entry, {"entry_sha256"})
                )
            write_jsonl(ledger_path, ledger)
            bytes_seal_path = tampered_raw_bytes / "remediation.json"
            bytes_seal = json.loads(bytes_seal_path.read_text())
            for relative in (
                "authorization-token.json",
                "authorization.json",
                "attempt-ledger.jsonl",
            ):
                digest = hashlib.sha256(
                    (tampered_raw_bytes / relative).read_bytes()
                ).hexdigest()
                bytes_seal["sealed_files"][relative] = digest
                bytes_seal["bundle_binding"]["sealed_files"][relative] = digest
            bytes_seal["bundle_binding"]["authorization_token_sha256"] = bytes_seal[
                "sealed_files"
            ]["authorization-token.json"]
            bytes_seal["bundle_binding"]["authorization_sha256"] = bytes_seal[
                "sealed_files"
            ]["authorization.json"]
            bytes_seal["bundle_binding"]["attempt_ledger_sha256"] = bytes_seal[
                "sealed_files"
            ]["attempt-ledger.jsonl"]
            bytes_seal["bundle_binding_sha256"] = compare_runs.canonical_sha256(
                bytes_seal["bundle_binding"]
            )
            write_json(bytes_seal_path, bytes_seal)
            with self.assertRaisesRegex(
                compare_runs.ArtifactError,
                "sealed rerun provenance is not the authorized provenance",
            ):
                compare_runs.verify_remediation_bundle(tampered_raw_bytes)

            tampered_evidence = base / "tampered-false-kill-evidence-bundle"
            shutil.copytree(bundle, tampered_evidence)
            evidence_relative = f"raw-target/{failure_relative}"
            write_json(
                tampered_evidence / evidence_relative,
                {"instance_id": target, "returncode": -9, "seconds": 5401.0},
            )
            reseal_remediation_bundle(tampered_evidence, evidence_relative)
            with self.assertRaisesRegex(
                compare_runs.ArtifactError, "false-kill evidence hash"
            ):
                compare_runs.verify_remediation_bundle(tampered_evidence)

            (bundle / "unsealed-second-attempt.json").write_text(
                "{}\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(compare_runs.ArtifactError, "unsealed"):
                compare_runs.verify_remediation_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
