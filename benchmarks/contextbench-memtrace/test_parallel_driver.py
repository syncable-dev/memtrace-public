import argparse
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("parallel_driver.py")
SPEC = importlib.util.spec_from_file_location(
    "contextbench_parallel_driver", MODULE_PATH
)
assert SPEC and SPEC.loader
driver = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = driver
SPEC.loader.exec_module(driver)


SUCCESS_RUNNER = r"""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--dataset")
parser.add_argument("--instance-id")
parser.add_argument("--output", type=Path)
parser.add_argument("--work-dir", type=Path)
parser.add_argument("--line-budget")
parser.add_argument("--selector-model")
parser.add_argument("--selector-mode")
parser.add_argument("--selector-policy")
parser.add_argument("--post-selector-policy")
parser.add_argument("--rerank-model-dir")
parser.add_argument("--reinclude-tracked-dirs", action="store_true")
parser.add_argument("--query-plan-file", type=Path)
parser.add_argument("--graph-cache-dir")
parser.add_argument("--cache-namespace")
args = parser.parse_args()
counter = args.output.parent / "invocations.txt"
count = int(counter.read_text() or "0") if counter.exists() else 0
counter.write_text(str(count + 1))
(args.output.parent / "selector-policy.txt").write_text(args.selector_policy or "")
(args.output.parent / "post-selector-policy.txt").write_text(
    args.post_selector_policy or "off"
)
args.output.parent.mkdir(parents=True, exist_ok=True)
prediction = {
    "instance_id": args.instance_id,
    "traj_data": {
        "pred_steps": [],
        "pred_files": ["src/example.py"],
        "pred_spans": {"src/example.py": [{"type": "line", "start": 1, "end": 2}]},
        "pred_symbols": {},
    },
    "model_patch": "",
}
args.output.write_text(json.dumps(prediction) + "\n")
audit_dir = args.output.parent / f"{args.output.stem}-audit"
audit_dir.mkdir(parents=True, exist_ok=True)
(audit_dir / f"{args.instance_id}.json").write_text("{}\n")
if args.query_plan_file:
    args.query_plan_file.write_text(json.dumps({args.instance_id: ["query"]}) + "\n")
"""


AGENT_SUCCESS_RUNNER = r"""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--dataset")
parser.add_argument("--instance-id")
parser.add_argument("--output-dir", type=Path)
parser.add_argument("--work-dir", type=Path)
parser.add_argument("--contextbench-root")
parser.add_argument("--agent-python")
parser.add_argument("--base-agent-config")
parser.add_argument("--rerank-model-dir")
parser.add_argument("--query-plan-file", type=Path)
parser.add_argument("--selector-model")
parser.add_argument("--agent-model")
parser.add_argument("--line-budget", type=int)
parser.add_argument("--timeout", type=int)
parser.add_argument("--history-days", type=int)
parser.add_argument("--cache-namespace")
parser.add_argument("--graph-cache-dir")
parser.add_argument("--fail-fast", action="store_true")
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
prediction = {
    "instance_id": args.instance_id,
    "traj_data": {
        "pred_steps": [],
        "pred_files": ["src/example.py"],
        "pred_spans": {"src/example.py": [{"type": "line", "start": 1, "end": 2}]},
        "pred_symbols": {},
    },
    "model_patch": "diff --git a/src/example.py b/src/example.py\n",
}
(args.output_dir / "predictions.jsonl").write_text(json.dumps(prediction) + "\n")
audit_dir = args.output_dir / "audit"
audit_dir.mkdir()
(audit_dir / f"{args.instance_id}.json").write_text(json.dumps({
    "agent": {
        "localization_protocol": {"policy": "hierarchy-listwise-v2"},
        "final_context_projection": {
            "policy": "rank-plus-scoped-recall-floor-v1",
            "line_budget": args.line_budget,
            "unique_lines": 2,
        },
    },
    "received_timeout": args.timeout,
}) + "\n")
args.query_plan_file.write_text(json.dumps({args.instance_id: ["query"]}) + "\n")
"""


def namespace(root: Path, fingerprint: str = "fingerprint-a") -> argparse.Namespace:
    dataset = root / "dataset.parquet"
    dataset.write_bytes(b"dataset")
    return argparse.Namespace(
        output_dir=root / "output",
        dataset=dataset,
        line_budget=80,
        selector_model=None,
        selector_mode="default",
        selector_policy=driver.DEFAULT_SELECTOR_POLICY,
        post_selector_policy="off",
        rerank_model_dir=None,
        graph_cache_dir=None,
        cache_namespace="contextbench-test-v1",
        reinclude_tracked_dirs=False,
        query_plans=False,
        timeout=5,
        resume=False,
        chdir=root,
        run_fingerprint=fingerprint,
    )


class ParallelDriverTests(unittest.TestCase):
    def run_one(self, runner: Path, args: argparse.Namespace) -> dict:
        results = {}
        with mock.patch.object(driver, "RUNNER", runner):
            driver.run_one("task-1", args, threading.Semaphore(1), results)
        return results["task-1"]

    def test_timeout_writes_zero_prediction_and_failure_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "slow_runner.py"
            runner.write_text("import time\ntime.sleep(60)\n")
            args = namespace(root)
            args.timeout = 0.1

            result = self.run_one(runner, args)

            run_dir = args.output_dir / "runs" / "task-1"
            prediction = json.loads((run_dir / "prediction.jsonl").read_text().strip())
            failure = json.loads((run_dir / "failure.json").read_text())
            record = json.loads((run_dir / "run_record.json").read_text())
            self.assertEqual(result["status"], "failure")
            self.assertEqual(result["failure_kind"], "timeout")
            self.assertEqual(prediction["traj_data"]["pred_files"], [])
            self.assertEqual(prediction["harness_failure"]["kind"], "timeout")
            self.assertEqual(failure["run_fingerprint"], "fingerprint-a")
            self.assertEqual(record["status"], "failure")

    def test_selector_policy_is_forwarded_to_the_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "success_runner.py"
            runner.write_text(SUCCESS_RUNNER)
            args = namespace(root)
            args.selector_policy = "continuation-v2"

            result = self.run_one(runner, args)

            policy_path = args.output_dir / "runs" / "task-1" / "selector-policy.txt"
            self.assertEqual(result["status"], "success")
            self.assertEqual(policy_path.read_text(), "continuation-v2")

    def test_post_selector_policy_is_forwarded_to_the_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "success_runner.py"
            runner.write_text(SUCCESS_RUNNER)
            args = namespace(root)
            args.post_selector_policy = "offline-packing-v2"

            result = self.run_one(runner, args)

            policy_path = (
                args.output_dir / "runs" / "task-1" / "post-selector-policy.txt"
            )
            self.assertEqual(result["status"], "success")
            self.assertEqual(policy_path.read_text(), "offline-packing-v2")

    def test_agent_lane_archives_prediction_and_sealed_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "agent_success_runner.py"
            runner.write_text(AGENT_SUCCESS_RUNNER)
            args = namespace(root)
            args.lane = "agent"
            args.contextbench_root = root / "contextbench"
            args.contextbench_root.mkdir()
            args.base_agent_config = root / "agent.yaml"
            args.base_agent_config.write_text("agent: test\n")
            args.rerank_model_dir = root / "rerank"
            args.rerank_model_dir.mkdir()
            args.selector_model = "gpt-5"
            args.agent_model = "openai/gpt-5"
            args.history_days = 365
            args.graph_cache_dir = root / "cache"
            args.cache_namespace = "agent-test-v1"
            args.query_plans = True
            args.timeout = 300

            results = {}
            with mock.patch.object(driver, "AGENT_RUNNER", runner):
                driver.run_one("task-1", args, threading.Semaphore(1), results)

            run_dir = args.output_dir / "runs" / "task-1"
            audit = json.loads(
                (run_dir / "prediction-audit" / "task-1.json").read_text()
            )
            prediction = json.loads((run_dir / "prediction.jsonl").read_text())
            self.assertEqual(results["task-1"]["status"], "success")
            self.assertEqual(prediction["instance_id"], "task-1")
            self.assertEqual(
                audit["agent"]["localization_protocol"]["policy"],
                "hierarchy-listwise-v2",
            )
            self.assertEqual(audit["received_timeout"], 180)

    def test_selector_policy_changes_the_resume_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = namespace(root)
            args.manifest = root / "manifest.json"
            args.manifest.write_text(json.dumps(["task-1"]) + "\n")
            args.concurrency = 4
            args.provenance_file = None

            replay = driver.build_run_provenance(args)
            args.selector_policy = "continuation-v2"
            continuation = driver.build_run_provenance(args)

            self.assertEqual(
                replay["policy"]["selector_policy"],
                driver.DEFAULT_SELECTOR_POLICY,
            )
            self.assertEqual(
                continuation["policy"]["selector_policy"],
                "continuation-v2",
            )
            self.assertNotEqual(replay["fingerprint"], continuation["fingerprint"])

    def test_post_selector_policy_changes_the_resume_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = namespace(root)
            args.manifest = root / "manifest.json"
            args.manifest.write_text(json.dumps(["task-1"]) + "\n")
            args.concurrency = 4
            args.provenance_file = None

            disabled = driver.build_run_provenance(args)
            args.post_selector_policy = "offline-packing-v2"
            enabled = driver.build_run_provenance(args)

            self.assertEqual(disabled["policy"]["post_selector_policy"], "off")
            self.assertIsNone(disabled["policy"]["post_selector_identity"])
            self.assertEqual(
                enabled["policy"]["post_selector_policy"],
                "offline-packing-v2",
            )
            self.assertEqual(
                enabled["policy"]["post_selector_identity"]["fingerprint"],
                driver.policy_fingerprint(line_budget=args.line_budget),
            )
            self.assertNotEqual(disabled["fingerprint"], enabled["fingerprint"])

    def test_resume_requires_matching_fingerprint_and_artifact_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "success_runner.py"
            runner.write_text(SUCCESS_RUNNER)
            args = namespace(root)
            args.query_plans = True

            first = self.run_one(runner, args)
            args.resume = True
            second = self.run_one(runner, args)
            args.run_fingerprint = "fingerprint-b"
            third = self.run_one(runner, args)

            counter = args.output_dir / "runs" / "task-1" / "invocations.txt"
            self.assertEqual(first["status"], "success")
            self.assertTrue(second["skipped"])
            self.assertFalse(third.get("skipped", False))
            self.assertEqual(counter.read_text(), "2")
            self.assertTrue(
                (
                    args.output_dir / "runs" / "task-1" / "query-plan.stale.json"
                ).is_file()
            )
            record = json.loads(
                (args.output_dir / "runs" / "task-1" / "run_record.json").read_text()
            )
            self.assertEqual(record["run_fingerprint"], "fingerprint-b")

    def test_resume_skips_a_matching_failure_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "failing_runner.py"
            runner.write_text(
                "from pathlib import Path\n"
                "p = Path(__file__).with_name('invocations.txt')\n"
                "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
                "raise SystemExit(7)\n"
            )
            args = namespace(root)

            first = self.run_one(runner, args)
            record_path = args.output_dir / "runs" / "task-1" / "run_record.json"
            frozen_hash = driver.sha256_file(record_path)
            args.resume = True
            second = self.run_one(runner, args)

            self.assertEqual(first["status"], "failure")
            self.assertEqual(second["status"], "failure")
            self.assertTrue(second["skipped"])
            self.assertEqual((root / "invocations.txt").read_text(), "1")
            self.assertEqual(driver.sha256_file(record_path), frozen_hash)

    def test_resume_rejects_a_tampered_failure_query_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "failing_after_plan.py"
            runner.write_text(SUCCESS_RUNNER + "\nraise SystemExit(7)\n")
            args = namespace(root)
            args.query_plans = True

            first = self.run_one(runner, args)
            run_dir = args.output_dir / "runs" / "task-1"
            query_plan = run_dir / "query-plan.json"
            query_plan.write_text('{"task-1":["tampered"]}\n')
            args.resume = True
            second = self.run_one(runner, args)

            self.assertEqual(first["status"], "failure")
            self.assertEqual(second["status"], "failure")
            self.assertFalse(second.get("skipped", False))
            self.assertEqual((run_dir / "invocations.txt").read_text(), "2")
            self.assertEqual(
                (run_dir / "query-plan.stale.json").read_text(),
                '{"task-1":["tampered"]}\n',
            )

    def test_tampered_prediction_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "success_runner.py"
            runner.write_text(SUCCESS_RUNNER)
            args = namespace(root)
            self.run_one(runner, args)
            prediction = args.output_dir / "runs" / "task-1" / "prediction.jsonl"
            prediction.write_text(prediction.read_text() + "{}\n")
            args.resume = True

            result = self.run_one(runner, args)

            counter = args.output_dir / "runs" / "task-1" / "invocations.txt"
            self.assertEqual(result["status"], "success")
            self.assertFalse(result.get("skipped", False))
            self.assertEqual(counter.read_text(), "2")

    def test_main_merges_failure_stub_and_keeps_failed_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "failing_runner.py"
            runner.write_text("raise SystemExit(7)\n")
            dataset = root / "dataset.parquet"
            dataset.write_bytes(b"dataset")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps(["task-1"]) + "\n")
            model_dir = root / "model"
            model_dir.mkdir()
            output_dir = root / "output"
            argv = [
                "parallel_driver.py",
                "--dataset",
                str(dataset),
                "--manifest",
                str(manifest),
                "--output-dir",
                str(output_dir),
                "--rerank-model-dir",
                str(model_dir),
                "--chdir",
                str(root),
            ]
            with (
                mock.patch.object(driver, "RUNNER", runner),
                mock.patch.object(sys, "argv", argv),
            ):
                returncode = driver.main()

            rows = [
                json.loads(line)
                for line in (output_dir / "predictions.jsonl").read_text().splitlines()
            ]
            summary = json.loads((output_dir / "driver_summary.json").read_text())
            provenance = json.loads((output_dir / "run_provenance.json").read_text())
            self.assertEqual(returncode, 1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["instance_id"], "task-1")
            self.assertEqual(rows[0]["traj_data"]["pred_files"], [])
            self.assertEqual(summary["completed"], 0)
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(summary["prediction_records"], 1)
            self.assertEqual(summary["total"], 1)
            self.assertEqual(
                provenance["policy"]["selector_policy"],
                driver.DEFAULT_SELECTOR_POLICY,
            )
            self.assertEqual(provenance["policy"]["post_selector_policy"], "off")


if __name__ == "__main__":
    unittest.main()
