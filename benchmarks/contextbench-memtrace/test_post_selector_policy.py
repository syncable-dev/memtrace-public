import ast
import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parent


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


policy = load_module("contextbench_post_selector_policy", "post_selector_policy.py")
offline = load_module(
    "contextbench_offline_packing_shared_policy", "offline_packing_counterfactual.py"
)
runner = load_module("contextbench_runner_post_selector_policy", "runner.py")


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


def prediction(files=None):
    selected = ["stale.py"] if files is None else list(files)
    return {
        "instance_id": "task-0",
        "traj_data": {
            "pred_files": selected,
            "pred_spans": {
                file: [{"start": 1, "end": 2, "type": "line"}]
                for file in selected
            },
            "pred_steps": [{"files": selected, "spans": {}, "symbols": {}}],
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


def complete_audit():
    selected = candidate(0, "src/core.py", 10, 19, "Core::run")
    closure = candidate(1, "src/core.py", 15, 24, "Core::run")
    support = candidate(2, "src/helper.py", 1, 5, "supportHelper")
    return {
        "search_queries": ["semantic one", "semantic two", "supportHelper"],
        "search_planner": {
            "source": "locked_query_plan",
            "identifier_queries": ["supportHelper"],
        },
        "searches": [
            search("semantic one", ["src/core.py", "src/helper.py"]),
            search("semantic two", ["src/helper.py"]),
            search("supportHelper", ["src/helper.py"]),
        ],
        "refinement_searches": [],
        "graph_contexts": [],
        "selector": {
            "selected_candidate_ids": [0],
            "candidate_pool": [selected, closure, support],
        },
        "final_candidates": [closure, support],
        "exact_identifier_candidates": [support],
        "lane_floor": [],
    }


class PostSelectorPolicyTests(unittest.TestCase):
    def test_runtime_and_sealed_replay_use_identical_projection(self):
        original = prediction()
        audit = complete_audit()

        sealed, sealed_evidence = offline.replay_prediction(
            original, audit, line_budget=40
        )
        runtime, runtime_evidence = policy.replay_prediction(
            original,
            audit,
            line_budget=40,
            strict_runtime_audit=True,
        )

        self.assertEqual(runtime, sealed)
        self.assertEqual(runtime_evidence, sealed_evidence)
        self.assertEqual(
            runtime["traj_data"]["pred_files"],
            ["src/core.py", "src/helper.py"],
        )
        self.assertEqual(
            runtime_evidence["projection_version"], policy.PROJECTION_VERSION
        )

    def test_off_is_the_exact_no_op(self):
        original = prediction()
        audit = complete_audit()
        prediction_bytes = json.dumps(original, separators=(",", ":")).encode()
        audit_bytes = json.dumps(audit, separators=(",", ":")).encode()

        projected, projected_audit = runner.apply_post_selector_policy(
            original,
            audit,
            policy_name="off",
            line_budget=40,
        )

        self.assertIs(projected, original)
        self.assertIs(projected_audit, audit)
        self.assertEqual(
            json.dumps(projected, separators=(",", ":")).encode(), prediction_bytes
        )
        self.assertEqual(
            json.dumps(projected_audit, separators=(",", ":")).encode(), audit_bytes
        )

    def test_enabled_runtime_preserves_raw_audit_and_records_evidence(self):
        original = prediction()
        audit = complete_audit()
        raw_audit = copy.deepcopy(audit)

        projected, projected_audit = runner.apply_post_selector_policy(
            original,
            audit,
            policy_name=policy.POLICY_VERSION,
            line_budget=40,
        )

        self.assertEqual(audit, raw_audit)
        for key, value in raw_audit.items():
            self.assertEqual(projected_audit[key], value)
        evidence = projected_audit["post_selector_policy"]
        self.assertEqual(
            evidence["fingerprint"], policy.policy_fingerprint(line_budget=40)
        )
        self.assertEqual(
            evidence["raw_prediction"]["context"],
            policy.prediction_context(original),
        )
        self.assertEqual(
            evidence["output_prediction"]["context"],
            policy.prediction_context(projected),
        )

    def test_incomplete_or_malformed_runtime_audit_fails_closed(self):
        audit = complete_audit()
        del audit["searches"]
        with self.assertRaisesRegex(
            policy.PostSelectorPolicyError, "no complete searches"
        ):
            policy.apply_runtime_policy(
                prediction(),
                audit,
                policy_name=policy.POLICY_VERSION,
                line_budget=40,
            )

        audit = complete_audit()
        audit["selector"]["selected_candidate_ids"] = [999]
        with self.assertRaisesRegex(
            policy.PostSelectorPolicyError, "outside its candidate pool"
        ):
            policy.apply_runtime_policy(
                prediction(),
                audit,
                policy_name=policy.POLICY_VERSION,
                line_budget=40,
            )

        audit = complete_audit()
        audit["final_candidates"].append("not-a-candidate")
        with self.assertRaisesRegex(
            policy.PostSelectorPolicyError, "malformed final_candidates"
        ):
            policy.apply_runtime_policy(
                prediction(),
                audit,
                policy_name=policy.POLICY_VERSION,
                line_budget=40,
            )

    def test_only_empty_projection_fails_open_to_nonempty_original(self):
        audit = complete_audit()
        audit["selector"] = {"selected_candidate_ids": [], "candidate_pool": []}
        audit["final_candidates"] = []
        audit["exact_identifier_candidates"] = []
        audit["lane_floor"] = []

        original = prediction()
        replayed, evidence = policy.replay_prediction(
            original,
            audit,
            line_budget=40,
            strict_runtime_audit=True,
        )
        self.assertEqual(replayed, original)
        self.assertEqual(evidence["status"], "replayed_fail_open_passthrough")

        empty = prediction(files=[])
        replayed, evidence = policy.replay_prediction(
            empty,
            audit,
            line_budget=40,
            strict_runtime_audit=True,
        )
        self.assertEqual(replayed["traj_data"]["pred_files"], [])
        self.assertEqual(evidence["status"], "replayed")

    def test_fingerprint_covers_source_parameters_projection_and_policy(self):
        baseline = policy.policy_fingerprint(
            line_budget=40, source_sha256="0" * 64
        )
        self.assertNotEqual(
            baseline,
            policy.policy_fingerprint(line_budget=40, source_sha256="1" * 64),
        )
        self.assertNotEqual(
            baseline,
            policy.policy_fingerprint(line_budget=41, source_sha256="0" * 64),
        )
        mutated = policy.PackingPolicy(support_max_new_candidates=3)
        self.assertNotEqual(
            baseline,
            policy.policy_fingerprint(
                line_budget=40,
                source_sha256="0" * 64,
                policy=mutated,
            ),
        )
        manifest = policy.policy_manifest(
            line_budget=40, source_sha256="0" * 64
        )
        self.assertEqual(manifest["projection"]["version"], policy.PROJECTION_VERSION)
        self.assertEqual(manifest["implementation"]["sha256"], "0" * 64)

    def test_v3_policy_is_explicit_and_keeps_v2_replay_identity_stable(self):
        self.assertIn(policy.POLICY_VERSION, policy.POST_SELECTOR_POLICY_CHOICES)
        self.assertIn(policy.POLICY_V3_VERSION, policy.POST_SELECTOR_POLICY_CHOICES)
        self.assertIn(policy.POLICY_V4_VERSION, policy.POST_SELECTOR_POLICY_CHOICES)
        self.assertFalse(policy.POLICY.support_selected_file_first)
        self.assertTrue(policy.POLICY_V3.support_selected_file_first)
        self.assertFalse(policy.POLICY.protect_causal_closure)
        self.assertTrue(policy.POLICY_V3.protect_causal_closure)
        self.assertFalse(policy.POLICY_V4.support_selected_file_first)
        self.assertTrue(policy.POLICY_V4.protect_causal_closure)
        self.assertEqual(policy.POLICY.as_dict()["version"], "offline-packing-v2")
        self.assertEqual(policy.POLICY_V3.as_dict()["version"], "offline-packing-v3")
        self.assertNotEqual(
            policy.policy_fingerprint(line_budget=40, policy=policy.POLICY),
            policy.policy_fingerprint(line_budget=40, policy=policy.POLICY_V3),
        )

    def test_v4_adds_causal_protection_without_v3_support_reordering(self):
        audit = complete_audit()
        producer = candidate(
            3, "src/core.py", 25, 29, "Core::prepare"
        )
        producer["shared_identifiers"] = ["shared_state_key"]
        audit["causal_closure"] = [producer]
        audit["final_candidates"].append(producer)

        v2, _ = policy.replay_prediction(
            prediction(), audit, line_budget=40, policy=policy.POLICY
        )
        v4, evidence = policy.replay_prediction(
            prediction(), audit, line_budget=40, policy=policy.POLICY_V4
        )

        self.assertNotIn(
            {"start": 25, "end": 29, "type": "line"},
            v2["traj_data"]["pred_spans"]["src/core.py"],
        )
        self.assertIn(
            {"start": 25, "end": 29, "type": "line"},
            v4["traj_data"]["pred_spans"]["src/core.py"],
        )
        self.assertEqual(
            evidence["selector"]["causal_protected_candidates"][0]["name"],
            "Core::prepare",
        )

    def test_policy_module_has_no_external_or_evaluator_imports(self):
        tree = ast.parse((ROOT / "post_selector_policy.py").read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".", 1)[0])
        self.assertLessEqual(
            roots,
            {
                "__future__",
                "collections",
                "copy",
                "dataclasses",
                "hashlib",
                "json",
                "math",
                "pathlib",
                "re",
                "typing",
            },
        )
        parameters = set(policy.replay_prediction.__annotations__)
        self.assertTrue({"prediction", "audit", "line_budget"} <= parameters)
        self.assertFalse({"gold", "evaluation", "metrics"} & parameters)

    def test_aws_launchers_forward_policy_and_reject_mixed_resume(self):
        config = (ROOT / "aws/config.env.example").read_text(encoding="utf-8")
        launcher = (ROOT / "aws/03-run.sh").read_text(encoding="utf-8")
        remote = (ROOT / "aws/remote/run-remote.sh").read_text(encoding="utf-8")

        self.assertIn(': "${POST_SELECTOR_POLICY:=off}"', config)
        self.assertIn(
            'INNER+=" POST_SELECTOR_POLICY=$(printf \'%q\' '
            '"$POST_SELECTOR_POLICY")"',
            launcher,
        )
        self.assertIn(': "${POST_SELECTOR_POLICY:=off}"', remote)
        canonical_export = (
            'export CB_POST_SELECTOR_POLICY="$POST_SELECTOR_POLICY"'
        )
        self.assertIn(canonical_export, remote)
        self.assertLess(
            remote.index(canonical_export),
            remote.index('"$VENV_PY" "${DRIVER_ARGS[@]}"'),
        )
        self.assertIn(
            'DRIVER_ARGS+=(--post-selector-policy "$POST_SELECTOR_POLICY")',
            remote,
        )
        self.assertIn(
            'if [ "$STORED_POST_SELECTOR_POLICY" != '
            '"$POST_SELECTOR_POLICY" ]; then',
            remote,
        )
        self.assertIn("policy changes require a fresh RUN_ID", remote)


if __name__ == "__main__":
    unittest.main()
