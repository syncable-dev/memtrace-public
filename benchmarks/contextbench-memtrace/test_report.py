import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("report.py")
SPEC = importlib.util.spec_from_file_location("contextbench_memtrace_report", MODULE_PATH)
assert SPEC and SPEC.loader
report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = report
SPEC.loader.exec_module(report)


def prediction(instance_id, failure=None):
    row = {"instance_id": instance_id, "traj_data": {"pred_steps": []}}
    if failure:
        row["harness_failure"] = {"kind": failure, "message": failure}
    return row


def metric_result(instance_id, value=1.0):
    metric = {"coverage": value, "precision": value}
    return {
        "instance_id": instance_id,
        "final": {"file": metric, "symbol": metric, "line": metric},
        "trajectory": {"auc_coverage": {"line": value}},
    }


def create_report(audit_dir, predictions, results, manifest, allow_partial=False):
    return report.create_report(
        predictions,
        results,
        manifest,
        audit_dir,
        "test-model",
        None,
        1.25,
        0.125,
        10.0,
        allow_partial=allow_partial,
    )


class ReportTests(unittest.TestCase):
    def test_context_metrics_macro_average_per_task_f1(self):
        results = [
            {"final": {"file": {"coverage": 1.0, "precision": 1.0}}},
            {"final": {"file": {"coverage": 0.0, "precision": 1.0}}},
        ]
        metrics = report.macro_context_metrics(results)["file"]
        self.assertEqual(metrics, {"recall": 0.5, "precision": 1.0, "f1": 0.5})
        self.assertNotAlmostEqual(metrics["f1"], report.harmonic_mean(0.5, 1.0))

    def test_trajectory_redundancy_uses_mean_per_step_overlap(self):
        predictions = [
            {
                "traj_data": {
                    "pred_steps": [
                        {
                            "spans": {
                                "a.py": [{"type": "line", "start": 1, "end": 10}]
                            }
                        },
                        {
                            "spans": {
                                "a.py": [{"type": "line", "start": 6, "end": 15}]
                            }
                        },
                    ]
                }
            }
        ]
        patterns = report.trajectory_patterns(predictions)
        self.assertEqual(patterns, {"steps": 2, "lines": 10, "redundancy": 0.5})

    def test_usage_cost_separates_cached_input(self):
        cost = report.usage_cost(
            {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20},
            input_price=1.25,
            cached_input_price=0.125,
            output_price=10.0,
        )
        self.assertAlmostEqual(cost, (60 * 1.25 + 40 * 0.125 + 20 * 10) / 1_000_000)

    def test_locked_query_plan_marks_cost_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_dir = Path(directory)
            (audit_dir / "task.json").write_text(
                '{"search_planner":{"source":"locked_query_plan"},'
                '"selector":{"input_tokens":100,"output_tokens":20}}',
                encoding="utf-8",
            )
            costs = report.audit_costs(audit_dir, 1.25, 0.125, 10.0)
            self.assertFalse(costs["complete"])
            self.assertIsNone(costs["average_usd"])
            self.assertEqual(costs["incomplete_instances"], ["task"])

    def test_agent_cost_is_added_to_retrieval_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_dir = Path(directory)
            (audit_dir / "task.json").write_text(
                '{"search_planner":{"input_tokens":100,"output_tokens":20},'
                '"selector":{"input_tokens":100,"output_tokens":20},'
                '"agent":{"cost_usd":0.5}}',
                encoding="utf-8",
            )
            costs = report.audit_costs(audit_dir, 1.25, 0.125, 10.0)
            retrieval_cost = 2 * (100 * 1.25 + 20 * 10.0) / 1_000_000
            self.assertAlmostEqual(costs["average_usd"], 0.5 + retrieval_cost)

    def test_evaluator_error_and_harness_failure_are_zero_in_manifest_macro(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_dir = Path(directory)
            (audit_dir / "task-a.json").write_text("{}\n", encoding="utf-8")
            (audit_dir / "task-b.json").write_text(
                '{"harness_failure":{"kind":"timeout"}}\n', encoding="utf-8"
            )

            output = create_report(
                audit_dir,
                [prediction("task-a"), prediction("task-b", "timeout")],
                [metric_result("task-a"), {"instance_id": "task-b", "error": "no_context_extracted"}],
                ["task-a", "task-b"],
            )

            self.assertEqual(output["leaderboard"]["line"]["f1"], 0.5)
            completeness = output["completeness"]
            self.assertTrue(completeness["artifact_complete"])
            self.assertTrue(completeness["score_publishable"])
            self.assertEqual(completeness["valid_evaluation_results"], 1)
            self.assertEqual(completeness["zero_accounted_instances"], 1)
            self.assertEqual(completeness["harness_failure_kinds"], {"timeout": 1})
            self.assertEqual(completeness["evaluator_error_results"], 1)
            self.assertIsNone(output["leaderboard"]["cost_usd"])

    def test_missing_result_fails_closed_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_dir = Path(directory)
            for instance_id in ("task-a", "task-b"):
                (audit_dir / f"{instance_id}.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(report.CompletenessError, "evaluation results is missing"):
                create_report(
                    audit_dir,
                    [prediction("task-a"), prediction("task-b")],
                    [metric_result("task-a")],
                    ["task-a", "task-b"],
                )

    def test_allow_partial_zero_fills_gap_and_marks_report_non_publishable(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_dir = Path(directory)
            (audit_dir / "task-a.json").write_text("{}\n", encoding="utf-8")
            output = create_report(
                audit_dir,
                [prediction("task-a")],
                [metric_result("task-a")],
                ["task-a", "task-b"],
                allow_partial=True,
            )
            self.assertEqual(output["leaderboard"]["file"]["f1"], 0.5)
            completeness = output["completeness"]
            self.assertFalse(completeness["artifact_complete"])
            self.assertFalse(completeness["score_publishable"])
            self.assertEqual(completeness["missing_predictions"], ["task-b"])
            self.assertEqual(completeness["missing_evaluation_results"], ["task-b"])
            self.assertEqual(completeness["zero_accounted_instances"], 1)
            self.assertIsNone(output["leaderboard"]["cost_usd"])

    def test_evaluation_synthesized_failure_is_never_publishable(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_dir = Path(directory)
            (audit_dir / "task-a.json").write_text(
                '{"harness_failure":{"kind":"missing_prediction"}}\n', encoding="utf-8"
            )
            failed_prediction = prediction("task-a", "missing_prediction")
            failed_prediction["harness_failure"]["run_fingerprint"] = "evaluation_merge"
            inputs = (
                audit_dir,
                [failed_prediction],
                [{"instance_id": "task-a", "error": "no_context_extracted"}],
                ["task-a"],
            )
            with self.assertRaisesRegex(report.CompletenessError, "synthesize failure"):
                create_report(*inputs)
            output = create_report(*inputs, allow_partial=True)
            completeness = output["completeness"]
            self.assertTrue(completeness["artifact_complete"])
            self.assertFalse(completeness["source_artifact_complete"])
            self.assertFalse(completeness["score_publishable"])
            self.assertEqual(
                completeness["evaluation_reconciled_failure_predictions"], ["task-a"]
            )

    def test_duplicate_or_extra_rows_are_never_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_dir = Path(directory)
            (audit_dir / "task-a.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(report.CompletenessError, "duplicate instance_id"):
                create_report(
                    audit_dir,
                    [prediction("task-a"), prediction("task-a")],
                    [metric_result("task-a")],
                    ["task-a"],
                    allow_partial=True,
                )
            with self.assertRaisesRegex(report.CompletenessError, "outside the manifest"):
                create_report(
                    audit_dir,
                    [prediction("task-a"), prediction("task-extra")],
                    [metric_result("task-a")],
                    ["task-a"],
                    allow_partial=True,
                )

    def test_manifest_loader_accepts_task_objects_and_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps({"tasks": [{"instance_id": "a"}, {"instance_id": "b"}]}))
            self.assertEqual(report.load_manifest_ids(path), ["a", "b"])
            path.write_text('["a","a"]')
            with self.assertRaisesRegex(report.CompletenessError, "duplicate instance_id"):
                report.load_manifest_ids(path)

    def test_pass_at_1_uses_the_same_manifest_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pass.jsonl"
            path.write_text('{"instance_id":"task-a","resolved":true}\n')
            with self.assertRaisesRegex(report.CompletenessError, "pass results is missing"):
                report.load_pass_at_1(path, ["task-a", "task-b"], allow_partial=False)
            value, observed, missing = report.load_pass_at_1(
                path, ["task-a", "task-b"], allow_partial=True
            )
            self.assertEqual(value, 0.5)
            self.assertEqual(observed, 1)
            self.assertEqual(missing, ["task-b"])


if __name__ == "__main__":
    unittest.main()
