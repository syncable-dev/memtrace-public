import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd


MODULE_PATH = Path(__file__).parents[1] / "aws" / "shard-orchestrator.py"
SPEC = importlib.util.spec_from_file_location("shard_orchestrator", MODULE_PATH)
ORCHESTRATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ORCHESTRATOR)


class ShardOrchestratorTests(unittest.TestCase):
    def test_dataset_kind_matches_remote_dataset_names(self):
        cases = {
            "/tmp/contextbench/data/contextbench_verified.parquet": "verified",
            "/tmp/contextbench/data/full.parquet": "full",
            "/tmp/contextbench/data/contextbench_verified_train.parquet": "train",
            "/tmp/contextbench/data/contextbench_verified_test.parquet": "test",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(ORCHESTRATOR.dataset_kind(path), expected)

    def test_dataset_kind_rejects_unknown_files(self):
        with self.assertRaisesRegex(ValueError, "unsupported ContextBench dataset"):
            ORCHESTRATOR.dataset_kind("/tmp/custom.parquet")

    def test_remote_dataset_path_matches_full_run(self):
        self.assertEqual(
            ORCHESTRATOR.remote_dataset_path("full"),
            "/srv/contextbench/contextbench/data/full.parquet",
        )

    def test_reusable_host_info_drops_old_run_state(self):
        reused = ORCHESTRATOR.reusable_host_info(
            {
                "instance_id": "i-1",
                "public_ip": "127.0.0.1",
                "volume_id": "vol-1",
                "run_id": "rejected-run",
                "preflight_run_id": "old-preflight",
                "bootstrap_error": "stale",
            }
        )
        self.assertEqual(
            reused,
            {"instance_id": "i-1", "public_ip": "127.0.0.1", "volume_id": "vol-1"},
        )

    def test_adopt_preserves_verified_memtrace_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "full.parquet"
            manifest = root / "manifest.json"
            source_path = root / "source.json"
            pd.DataFrame(
                [{"instance_id": "task-a", "repo": "owner/repository"}]
            ).to_parquet(dataset)
            manifest.write_text(json.dumps(["task-a"]) + "\n")
            source_identity = {
                "head_sha": "7f6caccf1c9513e3682298ac2d96caa350706591",
                "dirty_file_count": 0,
            }
            source_path.write_text(
                json.dumps(
                    {
                        "instance_type": "c7a.48xlarge",
                        "instance_vcpus": 192,
                        "requested_vcpus": 192,
                        "quota_vcpus": 1024,
                        "memtrace_source_provenance": source_identity,
                        "source_payload_sha256": "payload-sha256",
                        "shards": {
                            "shard-00": {
                                "instance_id": "i-1",
                                "public_ip": "127.0.0.1",
                                "volume_id": "vol-1",
                                "preflight_run_id": "old-preflight",
                            }
                        },
                    }
                )
                + "\n"
            )
            args = type(
                "Args",
                (),
                {
                    "adopt_state": str(source_path),
                    "shards": 1,
                    "dataset": str(dataset),
                    "manifest": str(manifest),
                    "run_tag": "repair",
                    "instance_type": "unused",
                    "concurrency": 12,
                    "root_volume_gb": 100,
                    "data_volume_gb": 100,
                },
            )()
            original = ORCHESTRATOR.FLEET_STATE_DIR
            ORCHESTRATOR.FLEET_STATE_DIR = root / "fleet"
            try:
                ORCHESTRATOR.cmd_adopt(args)
                adopted = json.loads(
                    (ORCHESTRATOR.FLEET_STATE_DIR / "repair" / "fleet.json").read_text()
                )
            finally:
                ORCHESTRATOR.FLEET_STATE_DIR = original

            self.assertEqual(adopted["memtrace_source_provenance"], source_identity)
            self.assertEqual(adopted["source_payload_sha256"], "payload-sha256")
            self.assertNotIn("preflight_run_id", adopted["shards"]["shard-00"])

    def test_preflight_records_probe_allows_workers_to_start(self):
        self.assertFalse(ORCHESTRATOR.preflight_records_ready("pending\n", "shard-00"))
        self.assertTrue(ORCHESTRATOR.preflight_records_ready("ready\n", "shard-00"))

    def test_preflight_records_probe_rejects_terminal_run_without_records(self):
        with self.assertRaisesRegex(RuntimeError, "terminated without a records directory"):
            ORCHESTRATOR.preflight_records_ready("terminal:1\n", "shard-00")

    def test_preflight_stop_is_scoped_to_expected_session_run(self):
        command = ORCHESTRATOR.preflight_stop_command("preflight-run-shard-00")
        self.assertIn("contextbench-preflight", command)
        self.assertIn("preflight-run-shard-00", command)
        self.assertIn("session does not match", command)
        self.assertIn('kill -TERM -- "-$pgid"', command)
        self.assertIn('kill -KILL -- "-$pgid"', command)

    def test_preflight_record_counts_keep_running_out_of_terminal_total(self):
        self.assertEqual(
            ORCHESTRATOR.preflight_record_counts(
                [
                    {"status": "success"},
                    {"status": "failure"},
                    {"status": "running"},
                    {"status": "running"},
                ]
            ),
            {"success": 1, "failure": 1, "running": 2},
        )

    def test_preflight_repair_manifest_excludes_assigned_fleet_tasks(self):
        records = [
            {"instance_id": "already-assigned", "status": "failure"},
            {"instance_id": "new-timeout", "status": "failure"},
            {"instance_id": "passed", "status": "success"},
            {"instance_id": "still-running", "status": "running"},
        ]

        actual = ORCHESTRATOR.unresolved_preflight_failures(
            records, {"already-assigned"}
        )

        self.assertEqual(actual, ["new-timeout"])

    def test_preflight_repair_exclusions_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            fleet_state = Path(directory)
            repair_dir = fleet_state / "repair-a"
            repair_dir.mkdir()
            (repair_dir / "fleet.json").write_text(
                json.dumps({"source_manifest": ["task-b", "task-a"]}) + "\n"
            )
            original = ORCHESTRATOR.FLEET_STATE_DIR
            ORCHESTRATOR.FLEET_STATE_DIR = fleet_state
            try:
                self.assertEqual(
                    ORCHESTRATOR.preflight_repair_exclusions(["repair-a"]),
                    {"task-a", "task-b"},
                )
                with self.assertRaisesRegex(
                    RuntimeError, "repair fleet state is missing"
                ):
                    ORCHESTRATOR.preflight_repair_exclusions(["missing"])
            finally:
                ORCHESTRATOR.FLEET_STATE_DIR = original

    def test_preflight_repair_proofs_override_remote_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            aggregate = Path(directory)
            records_dir = aggregate / "records"
            proofs_dir = aggregate / "repair-proofs"
            records_dir.mkdir()
            proofs_dir.mkdir()
            failed = {"instance_id": "task-a", "status": "failure"}
            passed = {
                "instance_id": "task-a",
                "status": "success",
                "cache": {"cache_hit": True},
                "stages": {
                    stage: {"status": "PASS"}
                    for stage in ORCHESTRATOR.PREFLIGHT_REQUIRED_STAGES
                },
            }
            (proofs_dir / "task-a.json").write_text(json.dumps(passed) + "\n")
            seen = {"task-a": failed}

            applied = ORCHESTRATOR.apply_preflight_repair_proofs(
                aggregate, records_dir, seen, ["task-a"]
            )

            self.assertEqual(applied, 1)
            self.assertEqual(seen["task-a"], passed)
            self.assertEqual(
                json.loads((records_dir / "task-a.json").read_text()), passed
            )

    def test_dataset_task_count_uses_full_dataset_not_subset_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "full.parquet"
            pd.DataFrame(
                [
                    {"instance_id": "task-a"},
                    {"instance_id": "task-b"},
                    {"instance_id": "task-c"},
                ]
            ).to_parquet(dataset)
            self.assertEqual(ORCHESTRATOR.dataset_task_count(dataset), 3)

    def test_full_tracker_dataset_survives_temporary_source_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "temporary" / "full.parquet"
            durable = root / "state" / "full.parquet"
            source.parent.mkdir()
            source.write_bytes(b"sealed-full-dataset")

            self.assertEqual(
                ORCHESTRATOR.ensure_full_tracker_dataset(source, durable), durable
            )
            self.assertEqual(durable.read_bytes(), b"sealed-full-dataset")

            source.unlink()
            self.assertEqual(
                ORCHESTRATOR.ensure_full_tracker_dataset(source, durable), durable
            )

    def test_full_tracker_dataset_fails_closed_when_all_copies_are_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "missing from both"):
                ORCHESTRATOR.ensure_full_tracker_dataset(
                    root / "temporary" / "full.parquet",
                    root / "state" / "full.parquet",
                )

    def test_full_tracker_dataset_rejects_snapshot_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "temporary" / "full.parquet"
            durable = root / "state" / "full.parquet"
            source.parent.mkdir()
            durable.parent.mkdir()
            source.write_bytes(b"new-dataset")
            durable.write_bytes(b"sealed-dataset")

            with self.assertRaisesRegex(RuntimeError, "dataset mismatch"):
                ORCHESTRATOR.ensure_full_tracker_dataset(source, durable)

    def test_full_run_gate_requires_every_task_and_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            aggregate = Path(directory)
            records = aggregate / "records"
            records.mkdir()
            fleet = {
                "source_manifest": ["task-a", "task-b"],
                "preflight_treatment": {
                    "dataset": "full",
                    "cache_namespace": "cache-v1",
                    "history_days": 0,
                },
            }
            summary = {
                "total": 2,
                "records": 2,
                "terminal_records": 2,
                "succeeded": 2,
                "failed": 0,
                "running": 0,
            }
            (aggregate / "summary.json").write_text(json.dumps(summary))
            stages = {
                name: {"status": "PASS"}
                for name in (
                    "dataset",
                    "checkout",
                    "index_embeddings",
                    "cache_sealed",
                    "mcp",
                )
            }
            for instance_id in fleet["source_manifest"]:
                (records / f"{instance_id}.json").write_text(
                    json.dumps(
                        {
                            "instance_id": instance_id,
                            "status": "success",
                            "stages": stages,
                        }
                    )
                )
            proof = ORCHESTRATOR.validate_full_preflight_gate(
                fleet, aggregate, "cache-v1"
            )
            self.assertEqual(proof["total"], 2)

    def test_full_run_gate_rejects_incomplete_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            aggregate = Path(directory)
            (aggregate / "records").mkdir()
            (aggregate / "summary.json").write_text(
                json.dumps(
                    {
                        "total": 2,
                        "records": 1,
                        "terminal_records": 1,
                        "succeeded": 1,
                        "failed": 0,
                        "running": 0,
                    }
                )
            )
            fleet = {
                "source_manifest": ["task-a", "task-b"],
                "preflight_treatment": {
                    "dataset": "full",
                    "cache_namespace": "cache-v1",
                    "history_days": 0,
                },
            }
            with self.assertRaisesRegex(RuntimeError, "has not passed"):
                ORCHESTRATOR.validate_full_preflight_gate(
                    fleet, aggregate, "cache-v1"
                )

    def test_full_run_gate_rejects_namespace_drift(self):
        fleet = {
            "source_manifest": ["task-a"],
            "preflight_treatment": {
                "dataset": "full",
                "cache_namespace": "old-cache",
                "history_days": 0,
            },
        }
        with self.assertRaisesRegex(RuntimeError, "treatment mismatch"):
            ORCHESTRATOR.validate_full_preflight_gate(
                fleet, Path("unused"), "new-cache"
            )


if __name__ == "__main__":
    unittest.main()
