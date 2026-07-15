import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "aws" / "full_preflight_runner.py"
SPEC = importlib.util.spec_from_file_location("full_preflight_runner", MODULE_PATH)
RUNNER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RUNNER)


class FullPreflightRunnerTests(unittest.TestCase):
    def test_task_query_is_deterministic_and_bounded(self):
        query = RUNNER.task_query("  alpha\n beta  " + "x" * 1000)
        self.assertTrue(query.startswith("alpha beta"))
        self.assertEqual(len(query), 600)

    def test_cache_progress_splits_checkout_from_indexing(self):
        stages = {
            "checkout": RUNNER.stage("RUNNING", "checkout"),
            "index_embeddings": RUNNER.stage("PENDING", ""),
        }
        RUNNER.apply_cache_progress(stages, "checkout_ready")
        self.assertEqual(stages["checkout"]["status"], "PASS")
        self.assertEqual(stages["index_embeddings"]["status"], "RUNNING")

    def test_manifest_loader_preserves_explicit_order(self):
        import pandas as pd

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "full.parquet"
            manifest = root / "manifest.json"
            pd.DataFrame({"instance_id": ["a", "b"]}).to_parquet(dataset)
            manifest.write_text(json.dumps(["b", "a"]))
            self.assertEqual(RUNNER.load_manifest(dataset, manifest), ["b", "a"])

    def test_pin_shim_preserves_real_binary_and_queues(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "memtrace"
            binary.write_bytes(b"\x7fELFfake")
            binary.chmod(0o755)
            real = RUNNER.install_pin_shim(binary)
            self.assertEqual(real.read_bytes(), b"\x7fELFfake")
            text = binary.read_text()
            self.assertIn(RUNNER.PIN_SHIM_MARKER, text)
            self.assertIn("MEMTRACE_PIN_WAIT_SECONDS", text)
            RUNNER.install_pin_shim(binary)
            self.assertEqual(real.read_bytes(), b"\x7fELFfake")

    def test_timeout_terminates_task_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid = root / "child.pid"
            command = [
                sys.executable,
                "-c",
                (
                    "import pathlib, subprocess, sys, time; "
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                    f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid)); "
                    "time.sleep(60)"
                ),
            ]
            args = Namespace(
                output_dir=root,
                resume=False,
                task_timeout=0.2,
                host="test-host",
                run_id="test-run",
            )
            with mock.patch.object(RUNNER, "child_command", return_value=command):
                _, returncode, _ = RUNNER.run_child(args, "timeout-task")

            self.assertEqual(returncode, 124)
            record = json.loads((root / "records" / "timeout-task.json").read_text())
            self.assertEqual(record["error"], "TimeoutExpired")
            pid = int(child_pid.read_text())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and Path(f"/proc/{pid}/stat").exists():
                state = Path(f"/proc/{pid}/stat").read_text().split()[2]
                if state == "Z":
                    break
                time.sleep(0.05)
            if Path(f"/proc/{pid}/stat").exists():
                self.assertEqual(Path(f"/proc/{pid}/stat").read_text().split()[2], "Z")
            else:
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def test_timeout_preserves_completed_stages_and_fails_running_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_path = root / "records" / "timeout-task.json"
            record_path.parent.mkdir()
            record_path.write_text(
                json.dumps(
                    {
                        "instance_id": "timeout-task",
                        "status": "running",
                        "stages": {
                            "dataset": RUNNER.stage("PASS", "metadata"),
                            "checkout": RUNNER.stage("PASS", "exact checkout"),
                            "index_embeddings": RUNNER.stage("RUNNING", "indexing"),
                            "cache_sealed": RUNNER.stage("PENDING", ""),
                            "mcp": RUNNER.stage("PENDING", ""),
                        },
                    }
                )
            )
            args = Namespace(
                output_dir=root,
                resume=False,
                task_timeout=0.1,
                host="test-host",
                run_id="test-run",
            )
            with mock.patch.object(
                RUNNER,
                "child_command",
                return_value=[sys.executable, "-c", "import time; time.sleep(60)"],
            ):
                RUNNER.run_child(args, "timeout-task")

            record = json.loads(record_path.read_text())
            self.assertEqual(record["stages"]["checkout"]["status"], "PASS")
            self.assertEqual(record["stages"]["index_embeddings"]["status"], "FAIL")
            self.assertEqual(record["stages"]["cache_sealed"]["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
