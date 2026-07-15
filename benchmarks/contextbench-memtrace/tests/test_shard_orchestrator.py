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
