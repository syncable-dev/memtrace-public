import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd


MODULE_PATH = Path(__file__).parents[1] / "aws" / "full_preflight_tracker.py"
SPEC = importlib.util.spec_from_file_location("full_preflight_tracker", MODULE_PATH)
TRACKER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(TRACKER)


class FullPreflightTrackerTests(unittest.TestCase):
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
