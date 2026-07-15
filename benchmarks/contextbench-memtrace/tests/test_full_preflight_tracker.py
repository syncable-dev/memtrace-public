import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import pandas as pd


MODULE_PATH = Path(__file__).parents[1] / "aws" / "full_preflight_tracker.py"
SPEC = importlib.util.spec_from_file_location("full_preflight_tracker", MODULE_PATH)
TRACKER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(TRACKER)


class FullPreflightTrackerTests(unittest.TestCase):
    def test_import_keeps_newest_observation_for_same_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "full.parquet"
            state = root / "state.jsonl"
            tracker = root / "TRACKER.md"
            newer_records = root / "newer"
            stale_records = root / "stale"
            newer_records.mkdir()
            stale_records.mkdir()
            pd.DataFrame(
                [
                    {
                        "instance_id": "task-a",
                        "repo": "owner/repo",
                        "repo_url": "https://example.invalid/owner/repo.git",
                        "language": "python",
                        "base_commit": "a" * 40,
                        "source": "Verified",
                    }
                ]
            ).to_parquet(dataset)
            (newer_records / "task-a.json").write_text(
                json.dumps(
                    {
                        "instance_id": "task-a",
                        "host": "repair-host",
                        "run_id": "repair-run",
                        "status": "running",
                        "updated_at_unix_ns": 200,
                        "stages": {
                            "dataset": {"status": "PASS", "detail": "ready"},
                            "checkout": {"status": "PASS", "detail": "exact checkout ready"},
                            "index_embeddings": {"status": "RUNNING", "detail": "indexing"},
                        },
                    }
                )
            )
            (stale_records / "task-a.json").write_text(
                json.dumps(
                    {
                        "instance_id": "task-a",
                        "host": "old-host",
                        "run_id": "old-run",
                        "status": "failure",
                        "completed_at_unix_ns": 100,
                        "stages": {
                            "dataset": {"status": "PASS", "detail": "ready"},
                            "checkout": {"status": "FAIL", "detail": "timed out"},
                        },
                    }
                )
            )
            common = {
                "dataset": dataset,
                "state": state,
                "tracker": tracker,
                "expected_total": 1,
            }
            for records_path in (newer_records, stale_records):
                TRACKER.command_import(argparse.Namespace(records=records_path, **common))
            record = TRACKER.load_state(state)["task-a"]
            self.assertEqual(record["host"], "repair-host")
            self.assertEqual(record["run_id"], "repair-run")
            self.assertEqual(record["preflight_observed_at_unix_ns"], 200)
            self.assertEqual(record["stages"]["checkout"]["status"], "PASS")
            self.assertEqual(record["stages"]["index_embeddings"]["status"], "RUNNING")

    def test_cli_waits_for_state_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "full.parquet"
            state = root / "state.jsonl"
            tracker = root / "TRACKER.md"
            pd.DataFrame(
                [
                    {
                        "instance_id": "task-a",
                        "repo": "owner/repo",
                        "repo_url": "https://example.invalid/owner/repo.git",
                        "language": "python",
                        "base_commit": "a" * 40,
                        "source": "Verified",
                    }
                ]
            ).to_parquet(dataset)
            command = [
                sys.executable,
                str(MODULE_PATH),
                "--dataset",
                str(dataset),
                "--state",
                str(state),
                "--tracker",
                str(tracker),
                "--expected-total",
                "1",
                "init",
            ]
            with TRACKER.state_lock(state):
                process = subprocess.Popen(command)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.05)
                self.assertIsNone(process.poll(), "tracker command bypassed the state lock")
                self.assertFalse(state.exists())
            process.wait(timeout=10)
            self.assertEqual(process.returncode, 0)
            self.assertTrue(state.exists())

    def test_reconcile_and_render_require_every_stage(self):
        frame = pd.DataFrame(
            [
                {
                    "instance_id": "task-a",
                    "repo": "owner/repo",
                    "repo_url": "https://example.invalid/owner/repo.git",
                    "language": "python",
                    "base_commit": "a" * 40,
                    "source": "Verified",
                }
            ]
        )
        records = TRACKER.reconcile(frame, {})
        record = records["task-a"]
        self.assertEqual(TRACKER.preflight_status(record), "PENDING")
        self.assertEqual(TRACKER.overall_status(record), "PENDING")
        for stage in TRACKER.PREFLIGHT_STAGES:
            record["stages"][stage]["status"] = "PASS"
        self.assertEqual(TRACKER.preflight_status(record), "PASS")
        self.assertEqual(TRACKER.overall_status(record), "PENDING")
        for stage in TRACKER.STAGES:
            record["stages"][stage]["status"] = "PASS"
        self.assertEqual(TRACKER.overall_status(record), "PASS")
        rendered = TRACKER.render(records, Path("full.parquet"), Path("state.jsonl"))
        self.assertIn("Preflight: **1 pass / 0 running / 0 fail / 0 pending**", rendered)
        self.assertIn("Final: **1 pass / 0 running / 0 fail / 0 pending**", rendered)
        self.assertIn("| task-a |", rendered)

    def test_state_round_trip_is_instance_keyed_and_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.jsonl"
            records = {
                "task-b": {"instance_id": "task-b"},
                "task-a": {"instance_id": "task-a"},
            }
            TRACKER.write_state(path, records)
            lines = path.read_text().splitlines()
            self.assertEqual(json.loads(lines[0])["instance_id"], "task-a")
            self.assertEqual(set(TRACKER.load_state(path)), {"task-a", "task-b"})


if __name__ == "__main__":
    unittest.main()
