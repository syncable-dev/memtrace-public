import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jinja2 import StrictUndefined, Template


MODULE_PATH = Path(__file__).with_name("agent_runner.py")
SPEC = importlib.util.spec_from_file_location(
    "contextbench_memtrace_agent_runner", MODULE_PATH
)
assert SPEC and SPEC.loader
agent_runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = agent_runner
SPEC.loader.exec_module(agent_runner)


class AgentRunnerTests(unittest.TestCase):
    def test_history_window_is_anchored_to_historical_snapshot(self):
        completed = agent_runner.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="1700000000\n", stderr=""
        )
        with (
            mock.patch.object(agent_runner.subprocess, "run", return_value=completed),
            mock.patch.object(
                agent_runner.time, "time", return_value=1700000000 + (900 * 86400)
            ),
        ):
            self.assertEqual(
                agent_runner.snapshot_anchored_days(Path("/repo"), 365), 1265
            )

    def test_benchmark_settings_cover_all_contextbench_families(self):
        self.assertEqual(
            agent_runner.benchmark_settings("Multi-SWE-Bench__go__x", "cli"),
            agent_runner.BenchmarkSettings(
                "multi-swe-bench", "train", "/home/cli", use_multi_config=True
            ),
        )
        self.assertEqual(
            agent_runner.benchmark_settings(
                "SWE-PolyBench__python__x", "org/repo"
            ).subset,
            "AmazonScience/SWE-PolyBench",
        )
        self.assertEqual(
            agent_runner.benchmark_settings(
                "SWE-Bench-Pro__python__x", "org/repo"
            ).subset,
            "pro",
        )
        self.assertEqual(
            agent_runner.benchmark_settings(
                "SWE-Bench-Verified__python__x", "org/repo"
            ).subset,
            "verified",
        )

    def test_seed_context_uses_exact_repository_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src/example.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
            context = agent_runner.render_seed_context(
                root,
                [{"file": "src/example.py", "start": 2, "end": 3}],
            )
            self.assertIn("File: /testbed/src/example.py", context)
            self.assertIn("Lines: 2-3", context)
            self.assertIn("two\nthree", context)
            self.assertNotIn("one", context)

    def test_merge_keeps_retrieval_then_agent_steps_and_agent_final(self):
        retrieval_prediction = {
            "traj_data": {"pred_steps": [{"files": ["seed.py"], "spans": {}}]}
        }
        agent_trajectory = {
            "pred_steps": [{"files": ["explore.py"], "spans": {}}],
            "pred_files": ["final.py"],
            "pred_spans": {"final.py": [{"type": "line", "start": 1, "end": 2}]},
        }
        merged = agent_runner.merge_prediction(
            "context-id", retrieval_prediction, agent_trajectory, "patch"
        )
        self.assertEqual(
            [step["files"] for step in merged["traj_data"]["pred_steps"]],
            [["seed.py"], ["explore.py"]],
        )
        self.assertEqual(merged["traj_data"]["pred_files"], ["final.py"])
        self.assertEqual(merged["model_patch"], "patch")

    def test_merge_uses_sealed_ranked_context_as_final_projection(self):
        merged = agent_runner.merge_prediction(
            "context-id",
            {"traj_data": {"pred_steps": []}},
            {
                "pred_steps": [],
                "pred_files": ["lossy.py"],
                "pred_spans": {"lossy.py": [{"type": "line", "start": 1, "end": 2}]},
            },
            "patch",
            [
                {
                    "action": "rank",
                    "arguments": {
                        "candidates": [
                            {"file": "src/a.py", "start": 10, "end": 20},
                            {"file": "src/b.py", "start": 30, "end": 40},
                        ]
                    },
                }
            ],
        )
        self.assertEqual(merged["traj_data"]["pred_files"], ["src/a.py", "src/b.py"])
        self.assertEqual(
            merged["traj_data"]["pred_spans"]["src/a.py"],
            [{"type": "line", "start": 10, "end": 20}],
        )

    def test_projection_preserves_high_confidence_scoped_symbol_per_shortlist_file(
        self,
    ):
        trace = [
            {
                "action": "shortlist",
                "arguments": {"files": ["src/a.py", "src/b.py"]},
            },
            {
                "action": "search",
                "arguments": {"file_path": "src/a.py"},
                "result": {
                    "results": [
                        {
                            "file_path": "src/a.py",
                            "kind": "Function",
                            "scope_path": "A::handle",
                            "symbol_start_line": 10,
                            "symbol_end_line": 20,
                            "score": 0.99,
                        }
                    ]
                },
            },
            {
                "action": "search",
                "arguments": {"file_path": "src/b.py"},
                "result": {
                    "results": [
                        {
                            "file_path": "src/b.py",
                            "kind": "Function",
                            "scope_path": "B::parse",
                            "symbol_start_line": 30,
                            "symbol_end_line": 40,
                            "score": 0.95,
                        }
                    ]
                },
            },
            {
                "action": "rank",
                "arguments": {
                    "candidates": [{"file": "src/a.py", "start": 12, "end": 18}]
                },
            },
        ]
        projection = agent_runner.project_agent_context(trace, 40)
        self.assertEqual(projection["unique_lines"], 22)
        self.assertEqual(
            projection["recall_floor_added"],
            [
                {
                    "file": "src/a.py",
                    "start": 10,
                    "end": 20,
                    "score": 0.99,
                    "name": "A::handle",
                },
                {
                    "file": "src/b.py",
                    "start": 30,
                    "end": 40,
                    "score": 0.95,
                    "name": "B::parse",
                },
            ],
        )

    def test_agent_config_requires_minimal_final_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base.yaml"
            output = root / "generated.yaml"
            base.write_text(
                "agent:\n"
                "  instance_template: 'Task: {{task}} </pr_description>'\n"
                "  context_request_template: 'Give context.'\n"
                "model: {}\n",
                encoding="utf-8",
            )
            agent_runner.write_agent_config(
                base, output, "File: /testbed/a.py", "openai/gpt-5"
            )
            generated = agent_runner.yaml.safe_load(output.read_text(encoding="utf-8"))
            self.assertIn(
                "memtrace_seed_context", generated["agent"]["instance_template"]
            )
            self.assertIn(
                "exact ranked production definitions",
                generated["agent"]["context_request_template"],
            )

    def test_agent_config_keeps_repository_jinja_delimiters_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base.yaml"
            output = root / "generated.yaml"
            base.write_text(
                "agent:\n"
                "  instance_template: 'Task: {{task}} </pr_description>'\n"
                "  context_request_template: 'Give context.'\n"
                "model: {}\n",
                encoding="utf-8",
            )
            seed = (
                "TabIndicatorProps={{ style: { backgroundColor: 'green' } }}\n"
                "literal = '{% endraw %}'\n"
                "comment = '{# not a template comment #}'"
            )

            agent_runner.write_agent_config(base, output, seed, "openai/gpt-5")

            generated = agent_runner.yaml.safe_load(output.read_text(encoding="utf-8"))
            rendered = Template(
                generated["agent"]["instance_template"],
                undefined=StrictUndefined,
            ).render(task="typescript regression")
            self.assertIn("Task: typescript regression", rendered)
            self.assertIn(seed, rendered)

    def test_agent_config_registers_live_hierarchical_memtrace_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base.yaml"
            output = root / "generated.yaml"
            wrapper = root / "docker-wrapper"
            base.write_text(
                "agent:\n"
                "  instance_template: 'Task: {{task}} </pr_description>'\n"
                "  context_request_template: 'Give context.'\n"
                "model: {}\n",
                encoding="utf-8",
            )
            agent_runner.write_agent_config(
                base,
                output,
                "File: /testbed/a.py",
                "openai/gpt-5",
                bridge_url="http://host.docker.internal:1234/tool",
                bridge_token="ephemeral",
                docker_executable=wrapper,
                line_budget=80,
            )
            generated = agent_runner.yaml.safe_load(output.read_text(encoding="utf-8"))
            prompt = generated["agent"]["instance_template"]
            self.assertIn("hierarchy-listwise-v2", prompt)
            self.assertIn("memtrace-agent shortlist", prompt)
            self.assertIn("memtrace-agent symbol", prompt)
            self.assertIn("memtrace-agent cochange", prompt)
            self.assertIn("memtrace-agent rank", prompt)
            self.assertIn("Never copy a placeholder", prompt)
            self.assertIn("80-line budget", prompt)
            self.assertEqual(generated["environment"]["executable"], str(wrapper))
            self.assertEqual(
                generated["environment"]["env"]["MEMTRACE_AGENT_URL"],
                "http://host.docker.internal:1234/tool",
            )
            self.assertIn(
                "memtrace-agent verify >/dev/null && echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                generated["agent"]["instance_template"],
            )
            self.assertIn(
                "memtrace-agent verify >/dev/null && echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                generated["agent"]["context_request_template"],
            )

    def test_protocol_validation_requires_ordered_hierarchy(self):
        trace = [
            {"action": "search", "arguments": {"query": "proxy auth returns 407"}},
            {"action": "shortlist", "arguments": {"files": ["a.py", "b.py"]}},
            {
                "action": "search",
                "arguments": {"query": "behavior", "file_path": "a.py"},
            },
            {
                "action": "search",
                "arguments": {"query": "behavior", "file_path": "b.py"},
            },
            {
                "action": "symbol",
                "arguments": {"symbol": "handle", "file_path": "a.py"},
            },
            {"action": "symbol", "arguments": {"symbol": "parse", "file_path": "b.py"}},
            {"action": "cochange", "arguments": {"target": "a.py"}},
            {
                "action": "rank",
                "arguments": {
                    "candidates": [
                        {"file": "a.py", "start": 10, "end": 20},
                        {"file": "b.py", "start": 30, "end": 40},
                    ]
                },
            },
        ]
        result = agent_runner.validate_agent_tool_trace(trace)
        self.assertEqual(result["policy"], "hierarchy-listwise-v2")
        self.assertEqual(result["history_sequence"], 7)
        self.assertEqual(result["rank_sequence"], 8)

    def test_protocol_validation_rejects_seed_only_or_flat_search(self):
        with self.assertRaisesRegex(RuntimeError, "listwise shortlist"):
            agent_runner.validate_agent_tool_trace(
                [
                    {
                        "action": "search",
                        "arguments": {"query": "proxy auth returns 407"},
                    },
                    {"action": "symbol", "arguments": {"symbol": "handle"}},
                ]
            )

    def test_ranked_candidates_must_be_grounded_and_fit_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src/a.py").write_text("\n" * 80, encoding="utf-8")
            bridge = object.__new__(agent_runner.AgentToolBridge)
            bridge.repo_root = root
            bridge.container_root = "/testbed"
            bridge.max_ranked_lines = 30
            bridge.trace = [
                {
                    "action": "search",
                    "result": {
                        "results": [
                            {"file_path": "src/a.py", "start_line": 10, "end_line": 50}
                        ]
                    },
                }
            ]
            self.assertEqual(
                bridge._ground_ranked_candidates(["src/a.py:10-20", "src/a.py:30-40"]),
                [
                    {"file": "src/a.py", "start": 10, "end": 20},
                    {"file": "src/a.py", "start": 30, "end": 40},
                ],
            )
            with self.assertRaisesRegex(ValueError, "not grounded"):
                bridge._ground_ranked_candidates(["src/a.py:1-2", "src/a.py:30-40"])

    def test_compact_search_result_omits_source_bodies(self):
        compacted = agent_runner.compact_tool_result(
            "search",
            {
                "query": "issue",
                "results": [
                    {
                        "file_path": "src/a.py",
                        "name": "handle",
                        "start_line": 10,
                        "end_line": 20,
                        "content": "secret source body",
                    }
                ],
            },
        )
        self.assertEqual(compacted["results"][0]["file_path"], "src/a.py")
        self.assertNotIn("content", compacted["results"][0])

    def test_docker_wrapper_mounts_bridge_client_and_host_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "docker-wrapper"
            client = root / "client.py"
            client.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            with mock.patch.object(
                agent_runner.shutil, "which", return_value="/usr/bin/docker"
            ):
                agent_runner.write_docker_wrapper(output, client)
            wrapper = output.read_text(encoding="utf-8")
            self.assertIn("host.docker.internal:host-gateway", wrapper)
            self.assertIn("/usr/local/bin/memtrace-agent:ro", wrapper)
            self.assertTrue(output.stat().st_mode & 0o111)

    def test_bridge_normalizes_container_paths_to_repo_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requests").mkdir()
            (root / "requests/utils.py").write_text("pass\n", encoding="utf-8")
            bridge = object.__new__(agent_runner.AgentToolBridge)
            bridge.repo_root = root
            bridge.container_root = "/testbed"
            self.assertEqual(
                bridge.normalize_agent_path("/testbed/requests/utils.py"),
                "requests/utils.py",
            )
            self.assertEqual(
                bridge.normalize_agent_path(str(root / "requests/utils.py")),
                "requests/utils.py",
            )

    def test_sanitize_patch_drops_build_output_but_keeps_source(self):
        patch = (
            "diff --git a/build/lib/a.py b/build/lib/a.py\nnew file mode 100644\n"
            "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        )
        cleaned, dropped = agent_runner.sanitize_patch(patch)
        self.assertNotIn("build/lib/a.py", cleaned)
        self.assertIn("src/a.py", cleaned)
        self.assertEqual(dropped, ["build/lib/a.py"])

    def test_sanitize_patch_drops_redis_runtime_files(self):
        patch = (
            "diff --git a/appendonly.aof b/appendonly.aof\nnew file mode 100644\n"
            "diff --git a/src/a.js b/src/a.js\n--- a/src/a.js\n+++ b/src/a.js\n"
        )
        cleaned, dropped = agent_runner.sanitize_patch(patch)
        self.assertNotIn("appendonly.aof", cleaned)
        self.assertIn("src/a.js", cleaned)
        self.assertEqual(dropped, ["appendonly.aof"])

    def test_sanitize_patch_drops_task_image_dockerfile(self):
        patch = (
            "diff --git a/Dockerfile b/Dockerfile\nnew file mode 100644\n"
            "diff --git a/src/a.ts b/src/a.ts\n--- a/src/a.ts\n+++ b/src/a.ts\n"
        )
        cleaned, dropped = agent_runner.sanitize_patch(patch)
        self.assertNotIn("Dockerfile", cleaned)
        self.assertIn("src/a.ts", cleaned)
        self.assertEqual(dropped, ["Dockerfile"])


if __name__ == "__main__":
    unittest.main()
