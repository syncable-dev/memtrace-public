import argparse
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd


MODULE_PATH = Path(__file__).with_name("aws") / "shard-orchestrator.py"
SPEC = importlib.util.spec_from_file_location(
    "contextbench_memtrace_shard_orchestrator", MODULE_PATH
)
assert SPEC and SPEC.loader
orchestrator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(orchestrator)


class ShardOrchestratorTests(unittest.TestCase):
    def test_source_payload_rejects_uninitialized_submodules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            fleet_state = root / "fleet"
            command = [
                "git",
                "--no-optional-locks",
                "-C",
                str(source),
                "submodule",
                "status",
                "--recursive",
            ]

            def fake_sh(actual, **_kwargs):
                self.assertEqual(actual, command)
                return subprocess.CompletedProcess(
                    actual,
                    0,
                    "-66a396b9de3520fa9d92aedb6b7d59d8ff867bc5 vendor/tantivy-memtrace\n",
                    "",
                )

            with (
                mock.patch.object(orchestrator, "MEMTRACE_SOURCE_DIR", source),
                mock.patch.object(orchestrator, "FLEET_STATE_DIR", fleet_state),
                mock.patch.object(orchestrator, "sh", side_effect=fake_sh),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "uninitialized submodules: vendor/tantivy-memtrace"
                ):
                    orchestrator.prepare_source_payload("test", {"dirty_file_count": 0})

    def test_build_shards_uses_only_the_exact_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset.parquet"
            manifest = root / "manifest.json"
            pd.DataFrame(
                {
                    "instance_id": ["a", "b", "c", "d"],
                    "repo": ["small", "large-vscode", "small", "small"],
                }
            ).to_parquet(dataset)
            manifest.write_text(json.dumps(["c", "a"]))

            shards, source_manifest = orchestrator.build_shards(2, dataset, manifest)

            self.assertEqual(source_manifest, ["c", "a"])
            self.assertEqual(sorted(sum(shards, [])), ["a", "c"])

    def test_collect_seals_cross_host_completion_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aws_dir = root / "aws"
            fleet_state = aws_dir / "state" / "fleet"
            remote_roots = {}
            source_manifest = [f"task-{index:02d}" for index in range(10)]
            shards = {}
            for shard_index in range(2):
                sid = f"shard-{shard_index:02d}"
                ip = f"host-{shard_index}"
                remote = root / "remote" / sid
                remote_roots[ip] = remote
                task_ids = source_manifest[shard_index::2]
                shards[sid] = {
                    "public_ip": ip,
                    "run_id": f"run-test-{sid}",
                    "task_ids": task_ids,
                }
                for instance_id in task_ids:
                    index = source_manifest.index(instance_id)
                    slug = instance_id
                    run_dir = remote / "runs" / slug
                    audit_dir = run_dir / "prediction-audit"
                    audit_dir.mkdir(parents=True)
                    (run_dir / "prediction.jsonl").write_text(
                        json.dumps({"instance_id": instance_id}) + "\n"
                    )
                    (audit_dir / f"{slug}.json").write_text("{}\n")
                    (run_dir / "query-plan.json").write_text("{}\n")
                    (run_dir / "run_record.json").write_text(
                        json.dumps(
                            {
                                "instance_id": instance_id,
                                "status": "success",
                                "completed_at_unix_ns": 10_000 - index,
                            }
                        )
                        + "\n"
                    )

            def fake_sh(command, **_kwargs):
                source = command[-2]
                destination = Path(command[-1])
                ip = source.split("@", 1)[1].split(":", 1)[0]
                shutil.copytree(remote_roots[ip], destination, dirs_exist_ok=True)
                return subprocess.CompletedProcess(command, 0, "", "")

            fleet = {
                "run_tag": "test",
                "source_manifest": source_manifest,
                "shards": shards,
            }
            with (
                mock.patch.object(orchestrator, "AWS_DIR", aws_dir),
                mock.patch.object(orchestrator, "FLEET_STATE_DIR", fleet_state),
                mock.patch.object(orchestrator, "sh", side_effect=fake_sh),
            ):
                orchestrator.save_fleet("test", fleet)
                orchestrator.cmd_collect(argparse.Namespace(run_tag="test", parallel=2))

            snapshot = json.loads(
                (
                    fleet_state
                    / "test"
                    / "aggregate"
                    / "snapshots"
                    / "terminal-0010.json"
                ).read_text()
            )
            self.assertEqual(len(snapshot["terminals"]), 10)
            self.assertEqual(snapshot["terminals"][0]["instance_id"], "task-09")
            self.assertEqual(snapshot["terminals"][-1]["instance_id"], "task-00")
            self.assertEqual(len(snapshot["files"]), 40)


if __name__ == "__main__":
    unittest.main()
