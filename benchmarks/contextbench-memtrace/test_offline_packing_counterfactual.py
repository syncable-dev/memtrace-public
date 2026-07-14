import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("offline_packing_counterfactual.py")
SPEC = importlib.util.spec_from_file_location(
    "contextbench_offline_packing_counterfactual", MODULE_PATH
)
assert SPEC and SPEC.loader
counterfactual = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = counterfactual
SPEC.loader.exec_module(counterfactual)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def candidate(identifier, file, start, end, name, kind="Function"):
    return {
        "id": identifier,
        "file": file,
        "start": start,
        "end": end,
        "name": name,
        "kind": kind,
        "retrieval_lanes": [],
    }


def prediction(instance_id="task-0"):
    return {
        "instance_id": instance_id,
        "traj_data": {
            "pred_files": ["stale.py"],
            "pred_spans": {"stale.py": [{"start": 1, "end": 2, "type": "line"}]},
            "pred_steps": [{"files": ["stale.py"], "spans": {}, "symbols": {}}],
            "pred_symbols": {},
        },
        "model_patch": "",
    }


def search(query, files):
    return {
        "query": query,
        "results": [
            {
                "file_path": f"/sealed/work/repo/{file}",
                "start_line": 1,
                "end_line": 5,
                "name": Path(file).stem,
                "kind": "Function",
            }
            for file in files
        ],
    }


class OfflinePackingCounterfactualTests(unittest.TestCase):
    def test_policy_exposes_fixed_task_agnostic_parameters(self):
        policy = counterfactual.POLICY.as_dict()
        self.assertEqual(policy["support_novel_line_cap"]["percent"], 25)
        self.assertEqual(policy["support_max_new_files"], 2)
        self.assertEqual(policy["support_max_new_candidates"], 2)
        self.assertEqual(policy["minimum_independent_query_families"], 2)
        self.assertEqual(policy["version"], "offline-packing-v2")
        self.assertTrue(
            policy["empty_replay_falls_back_to_original_nonempty_context"]
        )
        self.assertTrue(policy["same_scope_closure_first"])
        self.assertFalse(policy["task_specific_overrides"])
        self.assertFalse(policy["gold_or_evaluator_inputs"])

    def test_empty_replay_fails_open_to_original_nonempty_context(self):
        original = prediction()
        audit = {
            "search_queries": [],
            "search_planner": {"identifier_queries": []},
            "searches": [],
            "refinement_searches": [],
            "graph_contexts": [],
            "selector": {
                "selected_candidate_ids": [],
                "candidate_pool": [],
            },
            "final_candidates": [],
            "exact_identifier_candidates": [],
            "lane_floor": [],
        }

        replayed, replay_audit = counterfactual.replay_prediction(
            original, audit, line_budget=40
        )

        self.assertEqual(replayed, original)
        self.assertIsNot(replayed, original)
        self.assertEqual(
            replay_audit["status"], "replayed_fail_open_passthrough"
        )
        self.assertTrue(replay_audit["final"]["fail_open_to_original"])
        self.assertEqual(replay_audit["final"]["files"], ["stale.py"])

    def test_empty_replay_stays_empty_when_original_context_is_empty(self):
        original = prediction()
        original["traj_data"]["pred_files"] = []
        original["traj_data"]["pred_spans"] = {}
        audit = {
            "selector": {
                "selected_candidate_ids": [],
                "candidate_pool": [],
            },
            "final_candidates": [],
            "exact_identifier_candidates": [],
            "lane_floor": [],
        }

        replayed, replay_audit = counterfactual.replay_prediction(
            original, audit, line_budget=40
        )

        self.assertEqual(replayed["traj_data"]["pred_files"], [])
        self.assertEqual(replay_audit["status"], "replayed")
        self.assertFalse(replay_audit["final"]["fail_open_to_original"])

    def test_empty_replay_recognizes_span_only_original_context(self):
        original = prediction()
        original["traj_data"]["pred_files"] = []
        audit = {
            "selector": {
                "selected_candidate_ids": [],
                "candidate_pool": [],
            },
            "final_candidates": [],
            "exact_identifier_candidates": [],
            "lane_floor": [],
        }

        replayed, replay_audit = counterfactual.replay_prediction(
            original, audit, line_budget=40
        )

        self.assertEqual(replayed, original)
        self.assertEqual(
            replay_audit["status"], "replayed_fail_open_passthrough"
        )

    def test_selector_is_protected_then_closure_and_shared_support_caps_apply(self):
        pool = [
            candidate(0, "tests/test_core.py", 10, 19, "Service::run"),
            candidate(1, "tests/test_core.py", 15, 24, "Service::run"),
            candidate(2, "src/a.py", 1, 5, "helperAlpha"),
            candidate(3, "src/b.py", 1, 5, "helperBeta"),
            candidate(4, "src/c.py", 1, 5, "helperGamma"),
            candidate(5, "docs/readme.md", 1, 5, "documentation"),
        ]
        audit = {
            "search_queries": ["semantic one", "semantic two", "ExactName"],
            "search_planner": {"identifier_queries": ["ExactName"]},
            "searches": [
                search(
                    "semantic one",
                    ["src/a.py", "src/b.py", "src/c.py", "docs/readme.md"],
                ),
                search(
                    "semantic two",
                    ["src/a.py", "src/b.py", "src/c.py", "docs/readme.md"],
                ),
            ],
            "refinement_searches": [],
            "graph_contexts": [],
            "selector": {
                "selected_candidate_ids": [0],
                "candidate_pool": pool,
            },
            "final_candidates": pool[1:],
            "exact_identifier_candidates": [pool[2]],
            "lane_floor": [
                {key: value for key, value in item.items() if key != "id"}
                for item in pool[2:]
            ],
        }

        replayed, replay_audit = counterfactual.replay_prediction(
            prediction(), audit, line_budget=40
        )

        files = replayed["traj_data"]["pred_files"]
        self.assertEqual(files[0], "tests/test_core.py")
        self.assertIn(
            {"start": 10, "end": 19, "type": "line"},
            replayed["traj_data"]["pred_spans"]["tests/test_core.py"],
        )
        self.assertIn(
            {"start": 15, "end": 24, "type": "line"},
            replayed["traj_data"]["pred_spans"]["tests/test_core.py"],
        )
        self.assertEqual(replay_audit["final"]["support_candidates_added"], 2)
        self.assertEqual(replay_audit["final"]["support_new_files_added"], 2)
        self.assertLessEqual(replay_audit["final"]["support_novel_lines_added"], 10)
        self.assertNotIn("docs/readme.md", files)
        rejected = {
            row["candidate"]["file"]: row["reason"]
            for row in replay_audit["support"]
            if not row["accepted"]
        }
        self.assertEqual(rejected["docs/readme.md"], "non_production_support")
        self.assertEqual(rejected["src/c.py"], "support_candidate_cap")

    def test_refinement_and_graph_do_not_create_independent_query_families(self):
        selected = candidate(0, "src/core.py", 10, 19, "Core::run")
        support = candidate(1, "src/helper.py", 1, 5, "supportHelper")
        audit = {
            "search_queries": ["one semantic root", "supportHelper"],
            "search_planner": {"identifier_queries": ["supportHelper"]},
            "searches": [search("one semantic root", ["src/core.py"])],
            "refinement_searches": [
                {
                    **search("one semantic root", ["src/helper.py"]),
                    "refinement_file": "src/helper.py",
                }
            ],
            "graph_contexts": [
                {
                    "anchor": {
                        "file_path": "/sealed/work/repo/src/core.py",
                        "start_line": 10,
                        "end_line": 19,
                        "name": "Core::run",
                    },
                    "context": {
                        "callees": [
                            {
                                "file_path": "/sealed/work/repo/src/helper.py",
                                "start_line": 1,
                                "end_line": 5,
                                "name": "supportHelper",
                            }
                        ]
                    },
                }
            ],
            "selector": {
                "selected_candidate_ids": [0],
                "candidate_pool": [selected, support],
            },
            "final_candidates": [support],
            "exact_identifier_candidates": [],
            "lane_floor": [{key: value for key, value in support.items() if key != "id"}],
        }

        replayed, replay_audit = counterfactual.replay_prediction(
            prediction(), audit, line_budget=40
        )

        self.assertEqual(replayed["traj_data"]["pred_files"], ["src/core.py"])
        support_row = next(
            row
            for row in replay_audit["support"]
            if row["candidate"]["file"] == "src/helper.py"
        )
        self.assertEqual(support_row["independent_query_family_count"], 1)
        self.assertEqual(
            support_row["reason"], "insufficient_independent_query_families"
        )

    def test_gold_or_evaluator_keys_are_rejected(self):
        audit = {
            "metrics": {"line": 1.0},
            "selector": {"selected_candidate_ids": [], "candidate_pool": []},
        }
        with self.assertRaisesRegex(counterfactual.ArtifactError, "prohibited"):
            counterfactual.replay_prediction(prediction(), audit, line_budget=40)

    def test_exact_identifier_alone_is_not_corroboration(self):
        selected = candidate(0, "src/core.py", 10, 19, "Core::run")
        exact = candidate(1, "src/helper.py", 1, 5, "exactHelper")
        audit = {
            "search_queries": ["exactHelper"],
            "search_planner": {"identifier_queries": ["exactHelper"]},
            "searches": [search("exactHelper", ["src/helper.py"])],
            "refinement_searches": [],
            "graph_contexts": [],
            "selector": {
                "selected_candidate_ids": [0],
                "candidate_pool": [selected, exact],
            },
            "final_candidates": [exact],
            "exact_identifier_candidates": [exact],
            "lane_floor": [],
        }

        replayed, replay_audit = counterfactual.replay_prediction(
            prediction(), audit, line_budget=40
        )

        self.assertEqual(replayed["traj_data"]["pred_files"], ["src/core.py"])
        exact_row = next(row for row in replay_audit["support"])
        self.assertEqual(exact_row["sources"], ["exact"])
        self.assertEqual(exact_row["independent_query_family_count"], 0)
        self.assertEqual(
            exact_row["reason"], "insufficient_independent_query_families"
        )

    def test_manifest_ids_cannot_escape_the_audit_root(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory).resolve() / "manifest.json"
            write_json(manifest, ["../gold"])
            with self.assertRaisesRegex(counterfactual.ArtifactError, "safe path segment"):
                counterfactual._manifest_ids(manifest)
        with self.assertRaisesRegex(counterfactual.ArtifactError, "unsafe"):
            counterfactual._safe_relative_file("../gold.py")

    def test_pair_replay_reads_exact_ids_only_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manifest = root / "checkpoint" / "manifest.json"
            current_predictions = root / "checkpoint" / "candidate-predictions.jsonl"
            old_predictions = root / "checkpoint" / "control-predictions.jsonl"
            current_audits = root / "current-audits"
            old_audits = root / "old-audits"
            output = root / "counterfactual-n1"
            write_json(manifest, ["task-0"])
            write_jsonl(current_predictions, [prediction()])
            write_jsonl(old_predictions, [prediction()])
            selector_audit = {
                "search_queries": [],
                "search_planner": {"identifier_queries": []},
                "searches": [],
                "refinement_searches": [],
                "graph_contexts": [],
                "selector": {
                    "selected_candidate_ids": [0],
                    "candidate_pool": [
                        candidate(0, "src/core.py", 10, 19, "Core::run")
                    ],
                },
                "final_candidates": [],
                "exact_identifier_candidates": [],
                "lane_floor": [],
            }
            write_json(
                current_audits
                / "runs"
                / "task-0"
                / "prediction-audit"
                / "task-0.json",
                selector_audit,
            )
            write_json(old_audits / "task-0.json", selector_audit)
            # This simulates N+1. The explicit N=1 manifest means it must never be read.
            extra = current_audits / "runs" / "task-1" / "prediction-audit" / "task-1.json"
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.write_text("not json and contains gold", encoding="utf-8")

            result = counterfactual.replay_pair(
                ids_manifest=manifest,
                current_predictions=current_predictions,
                current_audits=current_audits,
                old_predictions=old_predictions,
                old_audits=old_audits,
                output_dir=output,
                line_budget=40,
            )

            self.assertEqual(result["ordered_ids"], ["task-0"])
            self.assertEqual(len(result["inputs"]["current_audits"]), 1)
            self.assertTrue((output / "current-predictions.jsonl").is_file())
            self.assertTrue((output / "old-predictions.jsonl").is_file())
            sealed = json.loads((output / "manifest.json").read_text())
            self.assertEqual(
                sealed["outputs"]["current-predictions.jsonl"],
                counterfactual._sha256_file(output / "current-predictions.jsonl"),
            )
            self.assertEqual(
                sealed["policy_identity"]["fingerprint"],
                counterfactual.policy_fingerprint(line_budget=40),
            )
            self.assertEqual(
                sealed["policy_identity"]["manifest"]["projection"]["version"],
                counterfactual.PROJECTION_VERSION,
            )
            with self.assertRaisesRegex(counterfactual.ArtifactError, "immutable"):
                counterfactual.replay_pair(
                    ids_manifest=manifest,
                    current_predictions=current_predictions,
                    current_audits=current_audits,
                    old_predictions=old_predictions,
                    old_audits=old_audits,
                    output_dir=output,
                    line_budget=40,
                )


if __name__ == "__main__":
    unittest.main()
