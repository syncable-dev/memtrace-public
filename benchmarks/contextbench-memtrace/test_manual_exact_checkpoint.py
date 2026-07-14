import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("aws") / "manual-exact-checkpoint.py"
SPEC = importlib.util.spec_from_file_location(
    "contextbench_manual_checkpoint", MODULE_PATH
)
assert SPEC and SPEC.loader
checkpoint = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checkpoint
SPEC.loader.exec_module(checkpoint)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


class ManualExactCheckpointTests(unittest.TestCase):
    def fixture(self, root: Path) -> argparse.Namespace:
        instance_id = "task-1"
        mirror = root / "mirror"
        prediction = mirror / "runs" / instance_id / "prediction.jsonl"
        audit = (
            mirror / "runs" / instance_id / "prediction-audit" / f"{instance_id}.json"
        )
        write_json(mirror / "manifest.json", [instance_id])
        write_jsonl(
            prediction,
            [{"instance_id": instance_id, "traj_data": {}, "model_patch": ""}],
        )
        write_json(
            audit,
            {
                "agent": {
                    "localization_protocol": {"policy": "hierarchy-listwise-v2"},
                    "final_context_projection": {
                        "policy": "rank-plus-scoped-recall-floor-v1",
                        "line_budget": 200,
                        "unique_lines": 63,
                    },
                }
            },
        )
        receipt = root / "terminal-0001.json"
        write_json(
            receipt,
            {
                "run_id": "run-agent-test",
                "terminals": [
                    {
                        "instance_id": instance_id,
                        "slug": instance_id,
                        "manifest_index": 0,
                    }
                ],
                "files": [
                    {
                        "path": prediction.relative_to(mirror).as_posix(),
                        "sha256": checkpoint.sha256(prediction),
                    },
                    {
                        "path": audit.relative_to(mirror).as_posix(),
                        "sha256": checkpoint.sha256(audit),
                    },
                ],
            },
        )
        for name in ("previous", "old"):
            control = root / name
            write_json(control / "manifest.json", [instance_id])
            write_jsonl(
                control / "predictions.jsonl",
                [{"instance_id": instance_id, "traj_data": {}, "model_patch": ""}],
            )
            write_jsonl(
                control / "results.jsonl",
                [
                    {
                        "instance_id": instance_id,
                        "final": {"line": {"coverage": 0.25, "precision": 0.5}},
                    }
                ],
            )
        return argparse.Namespace(
            mirror=mirror,
            snapshot_receipt=receipt,
            previous=root / "previous",
            old_control=root / "old",
            output=root / "checkpoint",
            count=1,
            treatment=None,
            agent_policy="hierarchy-listwise-v2",
            pack_policy=None,
            query_strategy=None,
            projection_policy="rank-plus-scoped-recall-floor-v1",
            line_budget=200,
        )

    def test_prepare_binds_agent_policy_and_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.fixture(Path(directory))

            checkpoint.prepare(args)

            binding = json.loads((args.output / "binding.json").read_text())
            self.assertEqual(binding["treatment"]["mode"], "agent")
            self.assertEqual(
                binding["treatment"]["agent_policy"], "hierarchy-listwise-v2"
            )
            self.assertEqual(binding["treatment"]["line_budget"], 200)

    def test_prepare_rejects_a_different_agent_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.fixture(Path(directory))
            args.agent_policy = "different-policy"

            with self.assertRaisesRegex(ValueError, "agent policy audit mismatch"):
                checkpoint.prepare(args)


if __name__ == "__main__":
    unittest.main()
