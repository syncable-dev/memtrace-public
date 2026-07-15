import importlib.util
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
