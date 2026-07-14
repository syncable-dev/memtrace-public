import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("reconcile_predictions.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("reconcile_predictions", MODULE_PATH)
assert SPEC and SPEC.loader
reconcile_predictions = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reconcile_predictions
SPEC.loader.exec_module(reconcile_predictions)


def write_prediction(root: Path, instance_id: str, row: dict) -> None:
    run_dir = root / "runs" / instance_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prediction.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")


class ReconcilePredictionsTests(unittest.TestCase):
    def test_reconcile_emits_one_record_per_manifest_task_and_explicit_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text('["task-a","task-b","task-c"]\n')
            write_prediction(
                root,
                "task-a",
                {"instance_id": "task-a", "traj_data": {"pred_steps": []}},
            )
            audit_a = root / "runs" / "task-a" / "prediction-audit"
            audit_a.mkdir()
            (audit_a / "task-a.json").write_text("{}\n", encoding="utf-8")
            write_prediction(
                root,
                "task-b",
                {"instance_id": "wrong-id", "traj_data": {"pred_steps": []}},
            )
            write_prediction(
                root,
                "task-c",
                {
                    "instance_id": "task-c",
                    "traj_data": {"pred_steps": []},
                    "harness_failure": {"kind": "timeout", "message": "too slow"},
                },
            )

            summary = reconcile_predictions.reconcile(root)

            predictions = [
                json.loads(line)
                for line in (root / "predictions.jsonl").read_text().splitlines()
            ]
            failures = [
                json.loads(line)
                for line in (root / "evaluation-failures.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                summary,
                {"manifest_instances": 3, "prediction_records": 3, "failure_records": 2},
            )
            self.assertEqual([row["instance_id"] for row in predictions], ["task-a", "task-b", "task-c"])
            self.assertEqual(predictions[1]["harness_failure"]["kind"], "invalid_prediction")
            self.assertEqual(predictions[2]["harness_failure"]["kind"], "timeout")
            self.assertEqual([row["kind"] for row in failures], ["invalid_prediction", "timeout"])
            self.assertEqual(
                sorted(path.name for path in (root / "predictions-audit").glob("*.json")),
                ["task-a.json", "task-b.json", "task-c.json"],
            )
            missing_audit = json.loads(
                (root / "predictions-audit" / "task-b.json").read_text()
            )
            self.assertEqual(missing_audit["harness_failure"]["kind"], "invalid_prediction")

    def test_duplicate_manifest_is_rejected_before_outputs_are_rebuilt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text('["task-a","task-a"]\n')
            with self.assertRaisesRegex(ValueError, "duplicate instance_id"):
                reconcile_predictions.reconcile(root)
            self.assertFalse((root / "predictions.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
