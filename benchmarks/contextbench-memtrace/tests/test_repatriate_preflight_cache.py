import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pandas as pd


MODULE_PATH = (
    Path(__file__).parents[1] / "aws" / "repatriate-preflight-cache.py"
)
SPEC = importlib.util.spec_from_file_location("repatriate_preflight_cache", MODULE_PATH)
REPATRIATE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(REPATRIATE)
ORCHESTRATOR_PATH = Path(__file__).parents[1] / "aws" / "shard-orchestrator.py"
ORCHESTRATOR_SPEC = importlib.util.spec_from_file_location(
    "repatriate_test_shard_orchestrator", ORCHESTRATOR_PATH
)
ORCHESTRATOR = importlib.util.module_from_spec(ORCHESTRATOR_SPEC)
assert ORCHESTRATOR_SPEC.loader is not None
ORCHESTRATOR_SPEC.loader.exec_module(ORCHESTRATOR)


class RepatriatePreflightCacheTests(unittest.TestCase):
    def test_ec2_network_identity_resolves_one_live_instance(self):
        payload = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "State": {"Name": "running"},
                            "PrivateIpAddress": "10.0.0.12",
                            "VpcId": "vpc-1",
                            "SecurityGroups": [{"GroupId": "sg-1"}],
                        }
                    ]
                }
            ]
        }
        with patch.object(
            REPATRIATE,
            "local_run",
            return_value=CompletedProcess([], 0, json.dumps(payload), ""),
        ):
            identity = REPATRIATE.ec2_network_identity("203.0.113.12")

        self.assertEqual(
            identity,
            {
                "private_ip": "10.0.0.12",
                "vpc_id": "vpc-1",
                "security_group_id": "sg-1",
            },
        )

    def test_terminal_fleet_accepts_nonzero_completed_run(self):
        fleet = {
            "shards": {
                "shard-00": {
                    "public_ip": "host",
                    "preflight_run_id": "run",
                }
            }
        }
        with patch.object(
            REPATRIATE,
            "remote_run",
            return_value=CompletedProcess([], 0, "1\n", ""),
        ):
            REPATRIATE.ensure_fleet_terminal(fleet, "ubuntu")

    def test_terminal_fleet_rejects_missing_exit_receipt(self):
        fleet = {
            "shards": {
                "shard-00": {
                    "public_ip": "host",
                    "preflight_run_id": "run",
                }
            }
        }
        with patch.object(
            REPATRIATE,
            "remote_run",
            return_value=CompletedProcess([], 1, "", "missing"),
        ):
            with self.assertRaisesRegex(RuntimeError, "not terminal"):
                REPATRIATE.ensure_fleet_terminal(fleet, "ubuntu")

    def test_publish_repair_proof_updates_destination_gate_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            fleet_dir = Path(directory) / "fleet" / "main"
            records_dir = fleet_dir / "aggregate-preflight" / "records"
            records_dir.mkdir(parents=True)
            fleet_path = fleet_dir / "fleet.json"
            fleet = {
                "source_manifest": ["task-a", "task-b"],
                "preflight_treatment": {
                    "dataset": "full",
                    "cache_namespace": "cache-v1",
                    "history_days": 0,
                },
            }
            fleet_path.write_text(json.dumps(fleet) + "\n")
            (records_dir / "task-a.json").write_text(
                json.dumps({"instance_id": "task-a", "status": "failure"}) + "\n"
            )
            stages = {
                stage: {"status": "PASS"}
                for stage in REPATRIATE.PREFLIGHT_REQUIRED_STAGES
            }
            (records_dir / "task-b.json").write_text(
                json.dumps(
                    {
                        "instance_id": "task-b",
                        "status": "success",
                        "stages": stages,
                    }
                )
                + "\n"
            )
            proof = {
                "instance_id": "task-a",
                "status": "success",
                "cache": {"cache_hit": True},
                "stages": stages,
            }

            summary = REPATRIATE.publish_repair_proof(fleet_path, proof)

            self.assertEqual(summary["succeeded"], 2)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["terminal_records"], 2)
            self.assertEqual(
                json.loads((records_dir / "task-a.json").read_text()), proof
            )
            self.assertTrue(
                (
                    fleet_dir
                    / "aggregate-preflight"
                    / "repair-proofs"
                    / "task-a.json"
                ).is_file()
            )
            gate = ORCHESTRATOR.validate_full_preflight_gate(
                fleet, fleet_dir / "aggregate-preflight", "cache-v1"
            )
            self.assertEqual(gate["total"], 2)

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
                        "preflight_run_id": "repair-run",
                        "task_ids": ["task-a"],
                    }
                },
            }
            destination = {
                "shards": {
                    "shard-03": {
                        "public_ip": "original",
                        "preflight_run_id": "original-run",
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
            self.assertEqual(
                plan[0]["destination_preflight_run_id"], "original-run"
            )
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
                        "preflight_run_id": "run",
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

    def test_destination_proof_requires_cache_hit_and_passed_mcp(self):
        task = {
            "instance_id": "task-a",
            "destination_host": "original",
            "destination_preflight_run_id": "original-run",
        }
        record = {
            "status": "success",
            "cache": {"cache_hit": True},
            "stages": {
                "checkout": {"status": "PASS"},
                "index_embeddings": {"status": "PASS"},
                "cache_sealed": {"status": "PASS"},
                "mcp": {"status": "PASS"},
            },
        }
        responses = [
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, json.dumps(record), ""),
        ]
        with patch.object(REPATRIATE, "remote_run", side_effect=responses) as remote:
            proof = REPATRIATE.verify_destination_task(
                task,
                "cache-v1",
                "/cache",
                "/memtrace",
                "ubuntu",
                "/data/full.parquet",
            )
        self.assertTrue(proof["cache"]["cache_hit"])
        command = remote.call_args_list[0].args[2]
        self.assertIn("--child", command)
        self.assertIn("--cache-namespace", command)
        self.assertIn("task-a", command)

    def test_destination_proof_rejects_rebuilt_cache(self):
        task = {
            "instance_id": "task-a",
            "destination_host": "original",
            "destination_preflight_run_id": "original-run",
        }
        record = {
            "status": "success",
            "cache": {"cache_hit": False},
            "stages": {"mcp": {"status": "PASS"}},
        }
        responses = [
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, json.dumps(record), ""),
        ]
        with patch.object(REPATRIATE, "remote_run", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "rebuilt"):
                REPATRIATE.verify_destination_task(
                    task,
                    "cache-v1",
                    "/cache",
                    "/memtrace",
                    "ubuntu",
                    "/data/full.parquet",
                )

    def test_execute_plan_reuses_cache_already_on_original_host(self):
        task = {
            "instance_id": "task-a",
            "repo_url": "https://example.test/repo.git",
            "base_commit": "a" * 40,
            "cache_key": "cache-key",
            "source_host": "same-host",
            "destination_host": "same-host",
            "destination_preflight_run_id": "original-run",
        }
        state = {"manifest_sha256": "manifest", "file_count": 42}
        proof = {
            "instance_id": "task-a",
            "status": "success",
            "cache": {"cache_hit": True},
        }
        with patch.object(
            REPATRIATE, "remote_sha256", return_value="binary"
        ), patch.object(
            REPATRIATE, "inspect_cache", return_value=state
        ) as inspect, patch.object(
            REPATRIATE, "validate_cache"
        ) as validate, patch.object(
            REPATRIATE, "stream_copy"
        ) as stream, patch.object(
            REPATRIATE, "promote_cache"
        ) as promote, patch.object(
            REPATRIATE, "verify_destination_task", return_value=proof
        ), patch.object(
            REPATRIATE,
            "publish_repair_proof",
            return_value={"succeeded": 1, "total": 1},
        ):
            REPATRIATE.execute_plan(
                [task],
                Path("fleet.json"),
                "cache-v1",
                "/cache",
                "/memtrace",
                "ubuntu",
                "/data/full.parquet",
            )

        self.assertEqual(inspect.call_count, 1)
        self.assertEqual(validate.call_count, 2)
        stream.assert_not_called()
        promote.assert_not_called()

    def test_execute_plan_routes_cross_host_copy_through_direct_vpc(self):
        task = {
            "instance_id": "task-a",
            "repo_url": "https://example.test/repo.git",
            "base_commit": "a" * 40,
            "cache_key": "cache-key",
            "source_host": "source-host",
            "destination_host": "destination-host",
            "destination_preflight_run_id": "original-run",
        }
        state = {"manifest_sha256": "manifest", "file_count": 42}
        proof = {
            "instance_id": "task-a",
            "status": "success",
            "cache": {"cache_hit": True},
        }
        with patch.object(
            REPATRIATE, "remote_sha256", return_value="binary"
        ), patch.object(
            REPATRIATE, "inspect_cache", return_value=state
        ), patch.object(
            REPATRIATE, "validate_cache"
        ), patch.object(
            REPATRIATE, "stream_copy"
        ) as operator_copy, patch.object(
            REPATRIATE, "stream_copy_direct_vpc"
        ) as direct_copy, patch.object(
            REPATRIATE, "promote_cache"
        ), patch.object(
            REPATRIATE, "verify_destination_task", return_value=proof
        ), patch.object(
            REPATRIATE,
            "publish_repair_proof",
            return_value={"succeeded": 1, "total": 1},
        ):
            REPATRIATE.execute_plan(
                [task],
                Path("fleet.json"),
                "cache-v1",
                "/cache",
                "/memtrace",
                "ubuntu",
                "/data/full.parquet",
                "direct-vpc",
            )

        operator_copy.assert_not_called()
        direct_copy.assert_called_once_with(
            "source-host",
            "destination-host",
            "ubuntu",
            "/cache/cache-key",
            direct_copy.call_args.args[4],
        )


if __name__ == "__main__":
    unittest.main()
