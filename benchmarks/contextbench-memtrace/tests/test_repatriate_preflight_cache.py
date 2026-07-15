import importlib.util
import tempfile
import unittest
from pathlib import Path

import pandas as pd


MODULE_PATH = (
    Path(__file__).parents[1] / "aws" / "repatriate-preflight-cache.py"
)
SPEC = importlib.util.spec_from_file_location("repatriate_preflight_cache", MODULE_PATH)
REPATRIATE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(REPATRIATE)


class RepatriatePreflightCacheTests(unittest.TestCase):
    def test_build_plan_restores_original_host_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "full.parquet"
            pd.DataFrame(
                [
                    {
                        "instance_id": "task-a",
                        "repo_url": "https://example.test/repo.git",
                        "base_commit": "a" * 40,
                    }
                ]
            ).to_parquet(dataset)
            repair = {
                "source_manifest": ["task-a"],
                "shards": {
                    "shard-00": {
                        "public_ip": "repair",
                        "task_ids": ["task-a"],
                    }
                },
            }
            destination = {
                "shards": {
                    "shard-03": {
                        "public_ip": "original",
                        "task_ids": ["task-a"],
                    }
                }
            }
            plan = REPATRIATE.build_plan(
                dataset,
                repair,
                destination,
                {"task-a": {"instance_id": "task-a", "status": "success"}},
                "cache-v1",
            )
            self.assertEqual(plan[0]["source_host"], "repair")
            self.assertEqual(plan[0]["destination_host"], "original")
            self.assertEqual(len(plan[0]["cache_key"]), 64)

    def test_build_plan_rejects_non_successful_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "full.parquet"
            pd.DataFrame(
                [
                    {
                        "instance_id": "task-a",
                        "repo_url": "https://example.test/repo.git",
                        "base_commit": "a" * 40,
                    }
                ]
            ).to_parquet(dataset)
            fleet = {
                "source_manifest": ["task-a"],
                "shards": {
                    "shard-00": {
                        "public_ip": "host",
                        "task_ids": ["task-a"],
                    }
                },
            }
            with self.assertRaisesRegex(ValueError, "not successful"):
                REPATRIATE.build_plan(
                    dataset,
                    fleet,
                    fleet,
                    {"task-a": {"instance_id": "task-a", "status": "running"}},
                    "cache-v1",
                )

    def test_validate_cache_checks_binary_and_exact_commit(self):
        task = {
            "instance_id": "task-a",
            "repo_url": "https://example.test/repo.git",
            "base_commit": "a" * 40,
        }
        state = {
            "manifest": {
                "repo_url": task["repo_url"],
                "base_commit": task["base_commit"],
                "namespace": "cache-v1",
                "history_days": 0,
                "memtrace_binary_sha256": "binary",
                "repository": {"commit_sha": task["base_commit"]},
            },
            "checkout_head": task["base_commit"],
            "checkout_status": "",
            "file_count": 2,
        }
        REPATRIATE.validate_cache(state, task, "cache-v1", "binary")
        state["manifest"]["memtrace_binary_sha256"] = "wrong"
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            REPATRIATE.validate_cache(state, task, "cache-v1", "binary")


if __name__ == "__main__":
    unittest.main()
